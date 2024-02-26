import requests
from pprint import pprint
from PyPDF2 import PdfReader, PdfMerger
import io
import string
import os


# Steg 1 : Logga in
# Steg 2 : Hämta json.publications från https://flippapi.egmontservice.com/api/refreshsignintoken
# Steg 3 : Välj Tidning, spara publication[n]["customPublicationCode"] som pubID
# Steg 4 : Välj specifik utgåva eller alla, spara publication[n][issues][n][customIssueCode] som issID
# Steg 5 : För vald utgåva, anropa https://reader.flipp.se/html5/reader/get_page_groups_from_eid.aspx?pubid=pubID&eid=issID
# Steg 6 : Lägg jason.pages[n][pdf] i lista
# Steg 7 : Ladda ner alla pdf-urler i listan och sammanfoga
# Steg 8 : Döp om filen


OUTPUTPATH = os.path.join(os.getcwd(), "Output")

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
		#"Accept": "application/json",
		#"Content-Type": "application/json",
		#"Host": "flippapi.egmontservice.com",
		#"Origin": "http://tidningar.flipp.se",
		#"Referer": "http://tidningar.flipp.se",
		# Lol, above not needed???
		"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0"
	}

	response = requests.post(url, json=payload, headers=headers).json()
	return response


def getPublicationsInfo(publications):
	publication_info = []
	for publication in publications["publications"]:
		publication_name = publication["name"]
		custom_publication_code = publication["customPublicationCode"]
		num_issues = len(getIssuesIds(custom_publication_code, publications))
		categories = [(category["id"], category["name"]) for category in publication.get("categories", [])]
		publication_info.append((publication_name, custom_publication_code, num_issues, categories))
	publication_info.sort(key = lambda x: x[2], reverse=True) #Publications with most issues first
	return publication_info


def getIssuesIds(publicationId, publications):
	for publication in publications["publications"]:
		if publication["customPublicationCode"] == publicationId:
			return [issue["customIssueCode"] for issue in publication["issues"]]


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


def getIssueInfoFromId(issueId, publicationId, publications):
	for publication in publications["publications"]:
		if publication["customPublicationCode"] == publicationId:
			for issue in publication["issues"]:
				if issue["customIssueCode"] == issueId:
					return issue["issueDate"], issue["issueName"]


def getIssuePDFs(publicationId, issueId):
	url = f"https://reader.flipp.se/html5/reader/get_page_groups_from_eid.aspx?pubid={publicationId}&eid={issueId}"
	# No auth! :)
	response = requests.get(url).json()
	pdf_urls = [page["pdf"] for group in response["pageGroups"] for page in group["pages"]]
	return pdf_urls


def readPdf(pdf):
	req = requests.get(url=pdf)
	if req.ok:
		return io.BytesIO(req.content)
	raise Exception(f"Error Code:  {req.status_code}")

def safeName(s):
	s = s.replace("/", "-")
	s = s.replace("&", "och")
	valid_chars = "-_.()åäöÅÄÖ %s%s" % (string.ascii_letters, string.digits)
	return ''.join(c for c in s if c in valid_chars)

def writePdf(pdfs, publicationFolder, issueName):
	publicationFolder = safeName(publicationFolder)
	issueName = safeName(issueName)

	outputFolder = os.path.join(OUTPUTPATH, publicationFolder)
	outputFile = os.path.join(outputFolder, issueName)

	if os.path.isfile(outputFile):
		print("File already exists")
		return
	
	merger = PdfMerger()
	for pdf in pdfs:
		merger.append(PdfReader(readPdf(pdf)))

	if not os.path.exists(outputFolder):
		os.mkdir(outputFolder)
	
	merger.write(outputFile)
	merger.close


def downloadAllIssues(publicationId, publications):
	name = getPublicationNameFromId(publicationId, publications)
	issues = getIssuesIds(publicationId, publications)

	for issue in issues:
		issueInfo = getIssueInfoFromId(issue, publicationId, publications)
		print(f"Downloading: {issue} - {name} - {issueInfo}")
		filename = f"{name} - {issueInfo[0]} - {issueInfo[1]}.pdf"
		writePdf(getIssuePDFs(publicationId, issue), name, filename)
		print(f"Written file: {filename}")
		print()


token = ""
publicationJson = getPublicationsJSON(token)

plist = getPublicationsInfo(publicationJson)
plist = filterbyCategory(plist, 52) #52 : Serietidningar

for publication in plist:
	downloadAllIssues(publication[1], publicationJson)
