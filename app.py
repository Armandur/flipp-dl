import requests
from pprint import pprint
from PyPDF2 import PdfReader, PdfMerger
import io
import string
import os
import sqlite3
import datetime
import argparse
import sys


# Steg 1 : Logga in
# Steg 2 : Hämta json.publications från https://flippapi.egmontservice.com/api/refreshsignintoken
# Steg 3 : Välj Tidning, spara publication[n]["customPublicationCode"] som pubID
# Steg 4 : Välj specifik utgåva eller alla, spara publication[n][issues][n][customIssueCode] som issID
# Steg 5 : För vald utgåva, anropa https://reader.flipp.se/html5/reader/get_page_groups_from_eid.aspx?pubid=pubID&eid=issID
# Steg 6 : Lägg jason.pages[n][pdf] i lista
# Steg 7 : Ladda ner alla pdf-urler i listan och sammanfoga
# Steg 8 : Döp om filen


OUTPUTPATH = os.path.join(os.getcwd(), "Output")
DB_PATH = os.path.join(os.getcwd(), "downloads.db")
REQUEST_TIMEOUT_SECS = 15

def init_db():
	# Skapar tabell för att hålla koll på redan nedladdade nummer
	with sqlite3.connect(DB_PATH) as conn:
		conn.execute("""
		CREATE TABLE IF NOT EXISTS issues_downloads (
			publication_code TEXT NOT NULL,
			issue_code TEXT NOT NULL,
			issue_date TEXT,
			issue_name TEXT,
			filename TEXT,
			downloaded_at TEXT,
			status TEXT NOT NULL DEFAULT 'downloaded',
			PRIMARY KEY (publication_code, issue_code)
		)
		""")

def is_downloaded(publication_code, issue_code):
	with sqlite3.connect(DB_PATH) as conn:
		cur = conn.execute("""
			SELECT 1 FROM issues_downloads
			WHERE publication_code=? AND issue_code=? AND status IN ('downloaded','archived')
		""", (publication_code, issue_code))
		return cur.fetchone() is not None

def mark_downloaded(publication_code, issue_code, issue_date, issue_name, filename):
	with sqlite3.connect(DB_PATH) as conn:
		conn.execute("""
		INSERT INTO issues_downloads (publication_code, issue_code, issue_date, issue_name, filename, downloaded_at, status)
		VALUES (?, ?, ?, ?, ?, ?, 'downloaded')
		ON CONFLICT(publication_code, issue_code) DO UPDATE SET
			issue_date=excluded.issue_date,
			issue_name=excluded.issue_name,
			filename=excluded.filename,
			downloaded_at=excluded.downloaded_at,
			status='downloaded'
		""", (publication_code, issue_code, issue_date, issue_name, filename, datetime.datetime.utcnow().isoformat()))

def mark_archived(publication_code, issue_code):
	with sqlite3.connect(DB_PATH) as conn:
		conn.execute("""
		UPDATE issues_downloads SET status='archived' WHERE publication_code=? AND issue_code=?
		""", (publication_code, issue_code))

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

	response = requests.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECS).json()
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
	response = requests.get(url, timeout=REQUEST_TIMEOUT_SECS).json()
	pdf_urls = [page["pdf"] for group in response["pageGroups"] for page in group["pages"]]
	return pdf_urls


def readPdf(pdf):
	req = requests.get(url=pdf, timeout=REQUEST_TIMEOUT_SECS)
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
		return False
	
	merger = PdfMerger()
	for pdf in pdfs:
		merger.append(PdfReader(readPdf(pdf)))

	if not os.path.exists(outputFolder):
		os.makedirs(outputFolder, exist_ok=True)
	
	merger.write(outputFile)
	merger.close()
	return True


def downloadAllIssues(publicationId, publications, *, skip_if_in_db=True, force=False):
	name = getPublicationNameFromId(publicationId, publications)
	publicationFolder = safeName(name)
	publicationFolder = os.path.join(OUTPUTPATH, publicationFolder)

	issues = getIssuesIds(publicationId, publications)

	for issue in issues:
		issueInfo = getIssueInfoFromId(issue, publicationId, publications)
		print(f"Downloading: {issue} - {name} - {issueInfo}")
		filename = safeName(f"{name} - {issueInfo[0]} - {issueInfo[1]}.pdf")
		# DB-kontroll först (om inte force)
		if skip_if_in_db and not force and is_downloaded(publicationId, issue):
			print("Already downloaded (DB)")
			continue
		# Filsystemskoll (om inte force)
		if not force and os.path.isfile(os.path.join(publicationFolder, filename)):
			print("File already exists")
			# Om fil finns men DB saknar rad, markera som nedladdad
			if skip_if_in_db and not is_downloaded(publicationId, issue):
				mark_downloaded(publicationId, issue, issueInfo[0], issueInfo[1], filename)
		else:
			ok = writePdf(getIssuePDFs(publicationId, issue), name, filename)
			if ok:
				mark_downloaded(publicationId, issue, issueInfo[0], issueInfo[1], filename)
				print(f"Written file: {filename}")
			else:
				print("Skipped writing (already exists)")
		print()


def parse_args():
	parser = argparse.ArgumentParser(description="flipp-dl - Ladda ner och slå ihop tidningsnummer från Flipp.")
	parser.add_argument("--token", help="API-token. Kan också sättas via env FLIPP_TOKEN.")
	parser.add_argument("--category", type=int, help="Kategori-ID att filtrera på (t.ex. 52).")
	parser.add_argument("--publication", help="Specifik publication customPublicationCode att ladda ner.")
	parser.add_argument("--output", help="Sökväg för utdata (Output-katalog).")
	parser.add_argument("--list-publications", action="store_true", help="Lista publikationer och avsluta.")
	parser.add_argument("--skip-if-in-db", action="store_true", default=True, help="Hoppa över om nedladdad enligt DB (default).")
	parser.add_argument("--no-skip-if-in-db", action="store_false", dest="skip_if_in_db", help="Inaktivera DB-skip.")
	parser.add_argument("--force", action="store_true", help="Ignorera DB och filsystemkontroll, ladda ner ändå.")
	return parser.parse_args()

def main():
	global OUTPUTPATH

	args = parse_args()

	# Token via arg eller env
	token = args.token or os.environ.get("FLIPP_TOKEN", "")
	if not token:
		print("Error: Token saknas. Ange --token eller sätt FLIPP_TOKEN i miljön.")
		sys.exit(1)

	# Output path
	if args.output:
		OUTPUTPATH = args.output

	# Init DB
	init_db()

	publicationJson = getPublicationsJSON(token)
	plist = getPublicationsInfo(publicationJson)

	if args.category is not None:
		plist = filterbyCategory(plist, args.category)

	if args.list_publications:
		pprint(plist)
		return

	# Om specifik publication angiven: ladda bara den
	if args.publication:
		downloadAllIssues(args.publication, publicationJson, skip_if_in_db=args.skip_if_in_db, force=args.force)
		return

	# Annars: loopa över filtrerad lista
	for publication in plist:
		downloadAllIssues(publication[1], publicationJson, skip_if_in_db=args.skip_if_in_db, force=args.force)

if __name__ == "__main__":
	main()
