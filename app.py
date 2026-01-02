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
import json


# Steg 1 : Logga in
# Steg 2 : Hämta json.publications från https://flippapi.egmontservice.com/api/refreshsignintoken
# Steg 3 : Välj Tidning, spara publication[n]["customPublicationCode"] som pubID
# Steg 4 : Välj specifik utgåva eller alla, spara publication[n][issues][n][customIssueCode] som issID
# Steg 5 : För vald utgåva, anropa https://reader.flipp.se/html5/reader/get_page_groups_from_eid.aspx?pubid=pubID&eid=issID
# Steg 6 : Lägg json.pages[n][pdf] i lista
# Steg 7 : Ladda ner alla pdf-urler i listan och sammanfoga
# Steg 8 : Döp om filen


OUTPUTPATH = os.path.join(os.getcwd(), "Output")
DB_PATH = os.path.join(os.getcwd(), "downloads.db")
REQUEST_TIMEOUT_SECS = 15

def print_header():
	print("\n==========================================================")
	print("                       FLIPP-DL")
	print("        Människohuggen grund • Finjusterad med vibbkodning")
	print("==========================================================\n")

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
		conn.execute("""
		CREATE TABLE IF NOT EXISTS download_jobs (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			job_type TEXT NOT NULL,
			publication_code TEXT NOT NULL,
			payload_json TEXT NOT NULL,
			status TEXT NOT NULL DEFAULT 'queued',
			created_at TEXT NOT NULL,
			started_at TEXT,
			finished_at TEXT,
			last_error TEXT
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

def enqueue_job(job_type, publication_code, payload: dict):
	with sqlite3.connect(DB_PATH) as conn:
		conn.execute("""
		INSERT INTO download_jobs (job_type, publication_code, payload_json, status, created_at)
		VALUES (?, ?, ?, 'queued', ?)
		""", (job_type, publication_code, json.dumps(payload), datetime.datetime.utcnow().isoformat()))

def list_jobs(status=None):
	with sqlite3.connect(DB_PATH) as conn:
		if status:
			cur = conn.execute("""
			SELECT id, job_type, publication_code, payload_json, status, created_at FROM download_jobs
			WHERE status = ? ORDER BY id ASC
			""", (status,))
		else:
			cur = conn.execute("""
			SELECT id, job_type, publication_code, payload_json, status, created_at FROM download_jobs
			ORDER BY id ASC
			""")
		return cur.fetchall()

def clear_jobs(status="queued"):
	with sqlite3.connect(DB_PATH) as conn:
		if status:
			conn.execute("""DELETE FROM download_jobs WHERE status = ?""", (status,))
		else:
			conn.execute("""DELETE FROM download_jobs""")

def delete_jobs_by_ids(job_ids):
	if not job_ids:
		return
	with sqlite3.connect(DB_PATH) as conn:
		qmarks = ",".join(["?"] * len(job_ids))
		conn.execute(f"DELETE FROM download_jobs WHERE id IN ({qmarks})", tuple(job_ids))

def update_job_payload(job_id, payload: dict):
	with sqlite3.connect(DB_PATH) as conn:
		conn.execute("""
		UPDATE download_jobs SET payload_json=? WHERE id=?
		""", (json.dumps(payload), job_id))

def run_jobs(publications, *, skip_if_in_db=True, force=False):
	with sqlite3.connect(DB_PATH) as conn:
		cur = conn.execute("""SELECT id, job_type, publication_code, payload_json FROM download_jobs WHERE status='queued' ORDER BY id ASC""")
		rows = cur.fetchall()
		for job_id, job_type, pub_code, payload_json in rows:
			started = datetime.datetime.utcnow().isoformat()
			conn.execute("""UPDATE download_jobs SET status='running', started_at=? WHERE id=?""", (started, job_id))
			try:
				payload = json.loads(payload_json or "{}")
				if job_type == "latest_n":
					n = int(payload.get("n", 1))
					downloadLatestNIssues(pub_code, publications, n, skip_if_in_db=skip_if_in_db, force=force)
				elif job_type == "range_indices":
					ranges = payload.get("ranges", [])
					issues_info = getIssuesForPublication(pub_code, publications)
					selected_info = []
					for rng in ranges:
						if not isinstance(rng, list) or len(rng) != 2:
							continue
						a, b = rng
						try:
							a = int(a); b = int(b)
						except Exception:
							continue
						if a > b:
							a, b = b, a
						for idx in range(max(1, a), min(len(issues_info), b) + 1):
							selected_info.append(issues_info[idx - 1])
					if selected_info:
						downloadIssuesSubset(pub_code, publications, selected_info, skip_if_in_db=skip_if_in_db, force=force)
				elif job_type == "date_range":
					date_from = payload.get("from")
					date_to = payload.get("to")
					selected_info = filterIssuesByDateRange(pub_code, publications, date_from, date_to)
					if selected_info:
						downloadIssuesSubset(pub_code, publications, selected_info, skip_if_in_db=skip_if_in_db, force=force)
				else:
					raise ValueError(f"Okänt job_type: {job_type}")
				finished = datetime.datetime.utcnow().isoformat()
				conn.execute("""UPDATE download_jobs SET status='done', finished_at=?, last_error=NULL WHERE id=?""", (finished, job_id))
			except Exception as e:
				finished = datetime.datetime.utcnow().isoformat()
				conn.execute("""UPDATE download_jobs SET status='failed', finished_at=?, last_error=? WHERE id=?""", (finished, str(e), job_id))

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

def getAllCategories(publications):
	# Returnerar unik lista av (id, name) över alla publikationer
	seen = {}
	for publication in publications["publications"]:
		for category in publication.get("categories", []):
			seen[category["id"]] = category["name"]
	cats = [(cid, seen[cid]) for cid in seen]
	cats.sort(key=lambda x: x[1].lower())
	return cats


def getIssueInfoFromId(issueId, publicationId, publications):
	for publication in publications["publications"]:
		if publication["customPublicationCode"] == publicationId:
			for issue in publication["issues"]:
				if issue["customIssueCode"] == issueId:
					return issue["issueDate"], issue["issueName"]

def getIssuesForPublication(publicationId, publications):
	# Returnerar lista av tuples (issueId, issueDate, issueName) sorterad nyast först på datumsträng
	issues_info = []
	for publication in publications["publications"]:
		if publication["customPublicationCode"] == publicationId:
			for issue in publication["issues"]:
				issues_info.append((issue["customIssueCode"], issue.get("issueDate", ""), issue.get("issueName", "")))
			break
	# Sortera på datum (string-jämförelse räcker om formatet är ISO-likt)
	issues_info.sort(key=lambda x: x[1], reverse=True)
	return issues_info

def parse_iso_date(s):
	try:
		return datetime.date.fromisoformat(s)
	except Exception:
		return None

def filterIssuesByDateRange(publicationId, publications, date_from, date_to):
	issues_info = getIssuesForPublication(publicationId, publications)
	df = parse_iso_date(date_from) if date_from else None
	dt = parse_iso_date(date_to) if date_to else None
	selected = []
	for issueId, issueDate, issueName in issues_info:
		idate = parse_iso_date(issueDate)
		ok = True
		if df and idate and idate < df:
			ok = False
		if dt and idate and idate > dt:
			ok = False
		# Om datum inte kunde parsas, behåll bara om inga filter gav restriktion
		if (df or dt) and idate is None:
			ok = False
		if ok:
			selected.append((issueId, issueDate, issueName))
	return selected


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

def downloadLatestNIssues(publicationId, publications, n, *, skip_if_in_db=True, force=False):
	# Hämtar de N senaste enligt datum och laddar ner i ordning nyast→äldst
	issues_info = getIssuesForPublication(publicationId, publications)
	if not issues_info:
		print("Inga nummer hittades för vald publikation.")
		return
	selected = issues_info[:max(0, int(n))]
	for issueId, issueDate, issueName in selected:
		# Återanvänd logik från downloadAllIssues men utan att räkna om info
		name = getPublicationNameFromId(publicationId, publications)
		publicationFolder = os.path.join(OUTPUTPATH, safeName(name))
		filename = safeName(f"{name} - {issueDate} - {issueName}.pdf")
		print(f"Downloading: {issueId} - {name} - ({issueDate}, {issueName})")
		if skip_if_in_db and not force and is_downloaded(publicationId, issueId):
			print("Already downloaded (DB)")
			continue
		if not force and os.path.isfile(os.path.join(publicationFolder, filename)):
			print("File already exists")
			if skip_if_in_db and not is_downloaded(publicationId, issueId):
				mark_downloaded(publicationId, issueId, issueDate, issueName, filename)
			continue
		ok = writePdf(getIssuePDFs(publicationId, issueId), name, filename)
		if ok:
			mark_downloaded(publicationId, issueId, issueDate, issueName, filename)
			print(f"Written file: {filename}")
		print()

def downloadIssuesSubset(publicationId, publications, issues_info_subset, *, skip_if_in_db=True, force=False):
	if not issues_info_subset:
		return
	name = getPublicationNameFromId(publicationId, publications)
	publicationFolder = os.path.join(OUTPUTPATH, safeName(name))
	for issueId, issueDate, issueName in issues_info_subset:
		filename = safeName(f"{name} - {issueDate} - {issueName}.pdf")
		print(f"Downloading: {issueId} - {name} - ({issueDate}, {issueName})")
		if skip_if_in_db and not force and is_downloaded(publicationId, issueId):
			print("Already downloaded (DB)")
			continue
		if not force and os.path.isfile(os.path.join(publicationFolder, filename)):
			print("File already exists")
			if skip_if_in_db and not is_downloaded(publicationId, issueId):
				mark_downloaded(publicationId, issueId, issueDate, issueName, filename)
			continue
		ok = writePdf(getIssuePDFs(publicationId, issueId), name, filename)
		if ok:
			mark_downloaded(publicationId, issueId, issueDate, issueName, filename)
			print(f"Written file: {filename}")
		print()


def parse_args():
	parser = argparse.ArgumentParser(description="flipp-dl - Ladda ner och slå ihop tidningsnummer från Flipp.")
	parser.add_argument("--token", help="API-token. Kan också sättas via env FLIPP_TOKEN.")
	parser.add_argument("--category", type=int, help="Kategori-ID att filtrera på (t.ex. 52).")
	parser.add_argument("--publication", help="Specifik publication customPublicationCode att ladda ner.")
	parser.add_argument("--output", help="Sökväg för utdata (Output-katalog).")
	parser.add_argument("--list-publications", action="store_true", help="Lista publikationer och avsluta.")
	parser.add_argument("--list-categories", action="store_true", help="Lista kategorier och avsluta.")
	parser.add_argument("--list-publications-in", type=int, help="Lista publikationer i angiven kategori och avsluta.")
	parser.add_argument("--interactive", action="store_true", help="Interaktivt läge: välj kategori och lista publikationer.")
	parser.add_argument("--list-jobs", action="store_true", help="Lista jobb i kön och avsluta.")
	parser.add_argument("--run-queue", action="store_true", help="Kör alla köade jobb.")
	parser.add_argument("--clear-jobs", action="store_true", help="Töm köade jobb (status=queued).")
	parser.add_argument("--skip-if-in-db", action="store_true", default=True, help="Hoppa över om nedladdad enligt DB (default).")
	parser.add_argument("--no-skip-if-in-db", action="store_false", dest="skip_if_in_db", help="Inaktivera DB-skip.")
	parser.add_argument("--force", action="store_true", help="Ignorera DB och filsystemkontroll, ladda ner ändå.")
	return parser.parse_args()

def main():
	global OUTPUTPATH

	def interactive_queue_manager(publications):
		while True:
			print("\n--- Jobbkö ---")
			rows = list_jobs()
			if not rows:
				print("Inga jobb i kön.")
			else:
				for idx, row in enumerate(rows, start=1):
					job_id, job_type, pub_code, payload_json, status, created_at = row
					try:
						payload = json.loads(payload_json or "{}")
					except Exception:
						payload = {}
					pub_name = getPublicationNameFromId(pub_code, publications) or pub_code
					print(f"[{idx}] id:{job_id} {status} - {job_type} - {pub_name} payload:{payload} ({created_at})")
			print("\nVälj åtgärd: [1] Kör kö [2] Töm kö [3] Ta bort valda [4] Ändra jobb [0] Tillbaka")
			choice = input("Ditt val: ").strip()
			if choice in ("0", ""):
				return
			if choice == "1":
				force_ans = input("Köra med force (ignorera DB och filsystemkontroll)? [j/N]: ").strip().lower()
				force_flag = force_ans in ("j","y","yes")
				skip_ans = input("Hoppa över enligt DB (skip-if-in-db)? [J/n]: ").strip().lower()
				skip_flag = not (skip_ans in ("n","no"))
				run_jobs(publications, skip_if_in_db=skip_flag, force=force_flag)
				print(f"Körning klar. (force={force_flag}, skip_if_in_db={skip_flag})")
			elif choice == "2":
				clear_jobs(status="queued")
				print("Tömde kö.")
			elif choice == "3":
				if not rows:
					continue
				val = input("Vilka jobb (t.ex. 1,3-4): ").strip()
				if not val:
					continue
				parts = [p.strip() for p in val.split(",") if p.strip()]
				sel = set()
				try:
					for part in parts:
						if "-" in part:
							a, b = part.split("-", 1); a = int(a); b = int(b)
							if a > b: a, b = b, a
							for x in range(a, b+1):
								sel.add(x)
						else:
							sel.add(int(part))
				except Exception:
					print("Ogiltigt urval."); continue
				job_ids = []
				for i in sorted(sel):
					if 1 <= i <= len(rows):
						job_ids.append(rows[i-1][0])
				delete_jobs_by_ids(job_ids)
				print(f"Tog bort {len(job_ids)} jobb.")
			elif choice == "4":
				if not rows:
					continue
				val = input("Vilket jobbnummer vill du ändra? ").strip()
				try:
					i = int(val)
					if not (1 <= i <= len(rows)):
						print("Ogiltigt nummer."); continue
				except Exception:
					print("Ogiltigt nummer."); continue
				job_id, job_type, pub_code, payload_json, status, created_at = rows[i-1]
				try:
					payload = json.loads(payload_json or "{}")
				except Exception:
					payload = {}
				print(f"Redigerar id:{job_id} typ:{job_type} payload:{payload}")
				if job_type == "latest_n":
					n_str = input("Nytt N (tomt = oförändrat): ").strip()
					if n_str:
						try:
							n = int(n_str); payload["n"] = n
						except Exception:
							print("Ogiltigt tal.")
					update_job_payload(job_id, payload)
					print("Uppdaterat.")
				elif job_type == "range_indices":
					span = input("Nytt intervall (t.ex. 1-5 eller 1,3-4,7). Tomt = oförändrat: ").strip()
					if span:
						new_ranges = []
						ok = True
						try:
							for part in [p.strip() for p in span.split(",") if p.strip()]:
								if "-" in part:
									a, b = part.split("-", 1); a = int(a); b = int(b)
									new_ranges.append([a, b])
								else:
									x = int(part); new_ranges.append([x, x])
						except Exception:
							ok = False
						if ok:
							payload["ranges"] = new_ranges
							update_job_payload(job_id, payload)
							print("Uppdaterat.")
						else:
							print("Ogiltigt format.")
				elif job_type == "date_range":
					df = input("Ny från-datum (YYYY-MM-DD, tomt = oförändrat): ").strip()
					dt = input("Ny till-datum (YYYY-MM-DD, tomt = oförändrat): ").strip()
					if df:
						payload["from"] = df
					if dt:
						payload["to"] = dt
					update_job_payload(job_id, payload)
					print("Uppdaterat.")
				else:
					print("Okänd jobbtyp; kan inte redigera.")
			else:
				print("Ogiltigt val.")

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

	# Jobbkö-hantering (icke-interaktiv)
	if args.list_jobs:
		rows = list_jobs()
		if not rows:
			print("Inga jobb i kön.")
		else:
			for row in rows:
				job_id, job_type, pub_code, payload_json, status, created_at = row
				try:
					payload = json.loads(payload_json or "{}")
				except Exception:
					payload = {}
				pub_name = getPublicationNameFromId(pub_code, publicationJson) or pub_code
				print(f"[{job_id}] {status} - {job_type} - {pub_name} ({pub_code}) payload:{payload} skapad:{created_at}")
		return
	if args.clear_jobs:
		clear_jobs(status="queued")
		print("Tömde köade jobb.")
		return
	if args.run_queue:
		run_jobs(publicationJson, skip_if_in_db=args.skip_if_in_db, force=args.force)
		print("Körning av kö avslutad.")
		return

	# Rent listläge (icke-interaktivt)
	if args.list_categories:
		cats = getAllCategories(publicationJson)
		for idx, (cid, cname) in enumerate(cats, start=1):
			print(f"[{idx}] {cname} (ID: {cid})")
		return
	if args.list_publications_in is not None:
		plist = filterbyCategory(plist, args.list_publications_in)
		for idx, (pname, pcode, num_issues, _cats) in enumerate(plist, start=1):
			print(f"[{idx}] {pname} ({num_issues} nummer)")
		return

	if args.category is not None:
		plist = filterbyCategory(plist, args.category)

	if args.interactive:
		# Huvudmeny
		while True:
			print_header()
			print("[1] Bläddra kategorier")
			print("[2] Hantera kö")
			print("[0] Avsluta")
			root = input("Ditt val: ").strip()
			if root in ("0",""):
				return
			if root == "2":
				interactive_queue_manager(publicationJson)
				continue
			if root != "1":
				print("Ogiltigt val."); continue
			# Kategorier
			while True:
				categories = getAllCategories(publicationJson)
				if not categories:
					print("Inga kategorier hittades.")
					break
				print("\nVälj kategori:\n")
				for idx, (cid, cname) in enumerate(categories, start=1):
					print(f"[{idx}] {cname} (ID: {cid})")
				print("\n[0] Till huvudmeny")
				val = input("Ditt val: ").strip()
				if val in ("0",""):
					break
				category_id = None
				if val.isdigit():
					i = int(val)
					if 1 <= i <= len(categories):
						category_id = categories[i-1][0]
				if category_id is None:
					print("Ogiltigt val."); continue
				# Publikationer i kategori
				go_main = False
				while True:
					plist_cat = filterbyCategory(getPublicationsInfo(publicationJson), category_id)
					if not plist_cat:
						print("Inga publikationer i vald kategori.")
						break
					print("\nPublikationer i vald kategori:\n")
					for idx, (pname, pcode, num_issues, _cats) in enumerate(plist_cat, start=1):
						print(f"[{idx}] {pname} ({num_issues} nummer)")
					print("\nVälj publikation ([siffra]) eller flera (t.ex. 1,3-5)")
					print("[0] Till kategorier   [H] Huvudmeny")
					val = input("Publikationsval: ").strip()
					if val in ("0",""):
						break
					if val.lower() == "h":
						go_main = True
						break
					selected_codes = None
					multi_mode = False
					if val.isdigit():
						i = int(val)
						if 1 <= i <= len(plist_cat):
							_, selected_pub_code, _, _ = plist_cat[i-1]
							selected_codes = [selected_pub_code]
							multi_mode = False
					if selected_codes is None:
						parts = [p.strip() for p in val.split(",") if p.strip()]
						indices = set()
						try:
							for part in parts:
								if "-" in part:
									a, b = part.split("-", 1); a = int(a); b = int(b)
									if a > b: a, b = b, a
									for x in range(a, b+1):
										indices.add(x)
								else:
									indices.add(int(part))
							valid = [i for i in indices if 1 <= i <= len(plist_cat)]
							if valid:
								selected_codes = [plist_cat[i-1][1] for i in sorted(valid)]
								multi_mode = True
						except ValueError:
							pass
					if not selected_codes:
						print("Ogiltigt val."); continue
					# Åtgärder
					if not multi_mode:
						while True:
							print("\nVälj åtgärd:\n[1] Lista nummer\n[2] Ladda ner senaste N\n[3] Lägg till i kö: senaste N\n[4] Lägg till i kö: indexintervall\n[5] Lägg till i kö: datumintervall\n[6] Hantera kö\n[7] Till kategorier\n[8] Till huvudmeny\n[0] Till publikationer")
							act = input("Ditt val: ").strip()
							if act in ("0",""):
								break
							if act == "7":
								break
							if act == "8":
								go_main = True
								break
							if act == "1":
								issues_info = getIssuesForPublication(selected_codes[0], publicationJson)
								if not issues_info:
									print("Inga nummer hittades."); continue
								print("\nNummer (nyast först):\n")
								for idx, (_iid, idate, iname) in enumerate(issues_info, start=1):
									print(f"[{idx}] {idate} - {iname}")
								print("\nKlart."); continue
							if act == "2":
								n_str = input("Hur många nummer vill du ladda ner? (t.ex. 1): ").strip()
								try:
									n = int(n_str) if n_str else 1
									if n <= 0: print("Ange ett tal > 0."); continue
								except ValueError:
									print("Ogiltigt tal."); continue
								downloadLatestNIssues(selected_codes[0], publicationJson, n, skip_if_in_db=True, force=False)
								print("Klart."); continue
							if act == "3":
								n_str = input("Hur många senaste vill du lägga till i kö? (t.ex. 1): ").strip()
								try:
									n = int(n_str) if n_str else 1
									if n <= 0: print("Ange ett tal > 0."); continue
								except ValueError:
									print("Ogiltigt tal."); continue
								enqueue_job("latest_n", selected_codes[0], {"n": n})
								print("Jobb tillagt."); continue
							if act == "4":
								span = input("Indexintervall (t.ex. 1-5 eller 1,3-4,7): ").strip()
								if not span: print("Inget intervall angivet."); continue
								ranges = []
								try:
									for part in [p.strip() for p in span.split(",") if p.strip()]:
										if "-" in part:
											a, b = part.split("-", 1); a = int(a); b = int(b)
											ranges.append([a, b])
										else:
											x = int(part); ranges.append([x, x])
								except Exception:
									print("Ogiltigt format."); continue
								enqueue_job("range_indices", selected_codes[0], {"ranges": ranges})
								print("Jobb tillagt."); continue
							if act == "5":
								df = input("Från-datum (YYYY-MM-DD, tomt = ingen nedre gräns): ").strip()
								dt = input("Till-datum (YYYY-MM-DD, tomt = ingen övre gräns): ").strip()
								enqueue_job("date_range", selected_codes[0], {"from": df or None, "to": dt or None})
								print("Jobb tillagt."); continue
							if act == "6":
								interactive_queue_manager(publicationJson); continue
							print("Ogiltigt val.")
					else:
						while True:
							print("\nValda publikationer:", ", ".join(selected_codes))
							print("Välj åtgärd för ALLA valda:\n[1] Lägg i kö: senaste N\n[2] Lägg i kö: indexintervall\n[3] Lägg i kö: datumintervall\n[4] Hantera kö nu\n[0] Till publikationer\n[H] Huvudmeny")
							act = input("Ditt val: ").strip()
							if act in ("0",""): break
							if act.lower() == "h": go_main = True; break
							if act == "1":
								while True:
									n_str = input("Hur många senaste nummer? (t.ex. 1): ").strip()
									try:
										n = int(n_str) if n_str else 1
										if n <= 0: print("Ange ett tal > 0."); continue
										break
									except ValueError:
										print("Ogiltigt tal. Försök igen.")
								for code in selected_codes:
									enqueue_job("latest_n", code, {"n": n})
								print(f"Lade till {len(selected_codes)} jobb i kön."); continue
							if act == "2":
								span = input("Indexintervall (t.ex. 1-5 eller 1,3-4,7): ").strip()
								ranges = []
								try:
									for part in [p.strip() for p in span.split(",") if p.strip()]:
										if "-" in part:
											a, b = part.split("-", 1); a = int(a); b = int(b)
											ranges.append([a, b])
										else:
											x = int(part); ranges.append([x, x])
								except Exception:
									print("Ogiltigt format."); continue
								for code in selected_codes:
									enqueue_job("range_indices", code, {"ranges": ranges})
								print(f"Lade till {len(selected_codes)} jobb i kön."); continue
							if act == "3":
								df = input("Från-datum (YYYY-MM-DD, tomt = ingen nedre gräns): ").strip()
								dt = input("Till-datum (YYYY-MM-DD, tomt = ingen övre gräns): ").strip()
								for code in selected_codes:
                                    # lägg varje jobb
									enqueue_job("date_range", code, {"from": df or None, "to": dt or None})
								print(f"Lade till {len(selected_codes)} jobb i kön."); continue
							if act == "4":
								interactive_queue_manager(publicationJson); continue
							print("Ogiltigt val.")
					if go_main: break
				if go_main: break
			# tillbaka till huvudmeny
			continue
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
