import io
import os
import string

import requests
from pypdf import PdfReader, PdfWriter


# Steg 1 : Logga in
# Steg 2 : Hämta json.publications från https://flippapi.egmontservice.com/api/refreshsignintoken
# Steg 3 : Välj Tidning, spara publication[n]["customPublicationCode"] som pubID
# Steg 4 : Välj specifik utgåva eller alla, spara publication[n][issues][n][customIssueCode] som issID
# Steg 5 : För vald utgåva, anropa https://reader.flipp.se/html5/reader/get_page_groups_from_eid.aspx?pubid=pubID&eid=issID
# Steg 6 : Lägg jason.pages[n][pdf] i lista
# Steg 7 : Ladda ner alla pdf-urler i listan och sammanfoga
# Steg 8 : Döp om filen


OUTPUTPATH = os.path.join(os.getcwd(), "Output")
REQUEST_TIMEOUT = 30


class FlippError(Exception):
	"""Raised when the Flipp API returns an unexpected response."""


def getPublicationsJSON(token, useruuid="dummy"): #Turns out user uuid isn't needed
	url = "https://flippapi.egmontservice.com/api/refreshsignintoken"
	payload = \
	{
		"email": "",
		"password": "",
		"token": token,
		"languageCulture": "sv-SE",
		"appId": "se.egmontmagasiner.flipp",
		"appVersion": "Landing Page",
		"uuid": useruuid,
		"os": "Firefox / Windows"
	}
	headers = \
	{
		"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0"
	}

	response = requests.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
	response.raise_for_status()
	data = response.json()
	if "publications" not in data:
		raise FlippError(
			"Flipp API response missing 'publications'. "
			"Is the token valid? Got keys: " + ", ".join(data.keys())
		)
	return data


def getPublicationsInfo(publications):
	publication_info = []
	for publication in publications["publications"]:
		publication_name = publication["name"]
		custom_publication_code = publication["customPublicationCode"]
		num_issues = len(publication.get("issues", []))
		categories = [(category["id"], category["name"]) for category in publication.get("categories", [])]
		publication_info.append((publication_name, custom_publication_code, num_issues, categories))
	publication_info.sort(key = lambda x: x[2], reverse=True) #Publications with most issues first
	return publication_info


def getIssuesIds(publicationId, publications):
	for publication in publications["publications"]:
		if publication["customPublicationCode"] == publicationId:
			return [issue["customIssueCode"] for issue in publication["issues"]]
	return []


def filterbyCategory(publicationsInfo, categoryId):
	filtered_list = []
	for publication in publicationsInfo:
		categories = publication[3]  # Extract categories from the tuple
		for category_id, category_name, in categories:
			if category_id == categoryId:
				filtered_list.append(publication)
				break  # No need to check other categories for this publication
	return filtered_list


def getPublicationNameFromId(publicationId, publications):
	for publication in publications["publications"]:
		if publication["customPublicationCode"] == publicationId:
			return publication["name"]
	return None


def getIssueInfoFromId(issueId, publicationId, publications):
	for publication in publications["publications"]:
		if publication["customPublicationCode"] == publicationId:
			for issue in publication["issues"]:
				if issue["customIssueCode"] == issueId:
					return issue["issueDate"], issue["issueName"]
	return None


def getIssuePDFs(publicationId, issueId):
	url = f"https://reader.flipp.se/html5/reader/get_page_groups_from_eid.aspx?pubid={publicationId}&eid={issueId}"
	# No auth! :)
	response = requests.get(url, timeout=REQUEST_TIMEOUT)
	response.raise_for_status()
	data = response.json()
	if "pageGroups" not in data:
		raise FlippError(
			f"Unexpected response for publication={publicationId} issue={issueId}: "
			f"missing 'pageGroups'"
		)
	return [page["pdf"] for group in data["pageGroups"] for page in group["pages"]]


def readPdf(pdf):
	response = requests.get(url=pdf, timeout=REQUEST_TIMEOUT)
	response.raise_for_status()
	return io.BytesIO(response.content)


def safeName(s):
	s = s.replace("/", "-")
	s = s.replace("&", "och")
	valid_chars = "-_.()åäöÅÄÖ %s%s" % (string.ascii_letters, string.digits)
	return ''.join(c for c in s if c in valid_chars)


def writePdf(pdfs, publicationFolder, issueName):
	outputFolder = os.path.join(OUTPUTPATH, publicationFolder)
	outputFile = os.path.join(outputFolder, issueName)

	os.makedirs(outputFolder, exist_ok=True)

	writer = PdfWriter()
	try:
		for pdf in pdfs:
			writer.append(PdfReader(readPdf(pdf)))
		with open(outputFile, "wb") as fh:
			writer.write(fh)
	finally:
		writer.close()


def downloadAllIssues(publicationId, publications):
	name = getPublicationNameFromId(publicationId, publications)
	if name is None:
		print(f"Unknown publication id: {publicationId}")
		return

	publicationFolder = safeName(name)
	publicationFolderPath = os.path.join(OUTPUTPATH, publicationFolder)

	issues = getIssuesIds(publicationId, publications)

	for issue in issues:
		issueInfo = getIssueInfoFromId(issue, publicationId, publications)
		if issueInfo is None:
			print(f"Skipping unknown issue: {issue}")
			continue
		print(f"Downloading: {issue} - {name} - {issueInfo}")
		filename = safeName(f"{name} - {issueInfo[0]} - {issueInfo[1]}.pdf")
		if os.path.isfile(os.path.join(publicationFolderPath, filename)):
			print("File already exists")
		else:
			writePdf(getIssuePDFs(publicationId, issue), publicationFolder, filename)
			print(f"Written file: {filename}")
		print()


def loadToken():
	"""Load the Flipp token from FLIPP_TOKEN env var or a local `token` file."""
	token = os.environ.get("FLIPP_TOKEN")
	if token:
		return token.strip()
	token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token")
	if os.path.isfile(token_file):
		with open(token_file, "r", encoding="utf-8") as fh:
			return fh.read().strip()
	return ""


def main():
	token = loadToken()
	if not token:
		raise SystemExit(
			"No Flipp token found. Set FLIPP_TOKEN or create a `token` file "
			"next to app.py. See README.md for details."
		)

	publicationJson = getPublicationsJSON(token)

	plist = getPublicationsInfo(publicationJson)
	plist = filterbyCategory(plist, 52) #52 : Serietidningar

	for publication in plist:
		downloadAllIssues(publication[1], publicationJson)


if __name__ == "__main__":
	main()
