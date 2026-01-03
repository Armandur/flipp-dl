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
import threading
import re
import time
import logging
from collections import deque


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
MAX_PAGE_WORKERS = 6
MAX_ISSUE_WORKERS = 3
SHOW_PROGRESS = True
PROGRESS_BAR_WIDTH = 30
DB_TIMEOUT_SECS = 30

progress_lock = threading.Lock()
db_write_lock = threading.Lock()
http_lock = threading.Lock()
last_request_ts = 0.0
RATE_LIMIT_RPS = 5  # 0 = av
http_session = None
AUTO_TUNE = True
_http_metrics = deque(maxlen=200)  # (ok:bool, elapsed:float, status:int)
_autotune_lock = threading.Lock()

def print_header():
	print("\n==========================================================")
	print("                       FLIPP-DL")
	print("        Människohuggen grund • Finjusterad med vibbkodning")
	print("==========================================================\n")

def render_progress(prefix, current, total, *, width=PROGRESS_BAR_WIDTH, start_ts=None, bytes_done=None):
	if not SHOW_PROGRESS or total <= 0:
		return
	current = min(current, total)
	filled = int(width * (current / float(total)))
	bar = "█" * filled + "-" * (width - filled)
	suffix = f"{current}/{total}"
	if start_ts:
		elapsed = max(0.0, time.time() - start_ts)
		if elapsed > 0:
			items_per_s = current / elapsed
			eta = (total - current) / items_per_s if items_per_s > 0 else 0
			mm = int(eta // 60); ss = int(eta % 60)
			suffix += f" • {items_per_s:.2f}/s ETA {mm:02d}:{ss:02d}"
			if bytes_done is not None and bytes_done > 0:
				mbps = (bytes_done / 1048576.0) / elapsed
				suffix += f" {mbps:.2f} MB/s"
	with progress_lock:
		print(f"\r{prefix} |{bar}| {suffix}", end="", flush=True)
	if current >= total:
		with progress_lock:
			print()

def setup_http_session():
	global http_session
	from requests.adapters import HTTPAdapter
	from urllib3.util.retry import Retry
	session = requests.Session()
	retry = Retry(
		total=5,
		connect=5,
		read=5,
		status=5,
		backoff_factor=0.5,
		status_forcelist=[429, 500, 502, 503, 504],
		allowed_methods=["GET", "POST"]
	)
	adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=50)
	session.mount("http://", adapter)
	session.mount("https://", adapter)
	http_session = session

def rate_limited_sleep():
	if RATE_LIMIT_RPS and RATE_LIMIT_RPS > 0:
		global last_request_ts
		now = time.time()
		with http_lock:
			min_interval = 1.0 / float(RATE_LIMIT_RPS)
			delay = last_request_ts + min_interval - now
			if delay > 0:
				time.sleep(delay)
			last_request_ts = time.time()

def http_get_json(url):
	rate_limited_sleep()
	t0 = time.time()
	resp = http_session.get(url, timeout=REQUEST_TIMEOUT_SECS)
	resp.raise_for_status()
	t1 = time.time()
	record_http_result(True, t1 - t0, resp.status_code)
	return resp.json()

def http_post_json(url, payload, headers):
	rate_limited_sleep()
	t0 = time.time()
	resp = http_session.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECS)
	resp.raise_for_status()
	t1 = time.time()
	record_http_result(True, t1 - t0, resp.status_code)
	return resp.json()

def record_http_result(ok, elapsed, status):
	try:
		with _autotune_lock:
			_http_metrics.append((ok, float(elapsed), int(status or 0)))
			# Tona endast vid jämna intervaller för låg overhead
			if AUTO_TUNE and len(_http_metrics) % 25 == 0:
				autotune_rate_limit()
	except Exception:
		pass

def autotune_rate_limit():
	global RATE_LIMIT_RPS, MAX_PAGE_WORKERS
	if RATE_LIMIT_RPS == 0:
		return  # av
	if not _http_metrics:
		return
	window = list(_http_metrics)[-50:]  # senaste 50
	err = sum(1 for ok, _, _ in window if not ok)
	err_rate = err / float(len(window))
	lat = [elapsed for ok, elapsed, _ in window]
	avg_lat = sum(lat) / float(len(lat))
	has_429 = any(status == 429 for _, _, status in window)
	# Enkla regler: för hög latens eller 429 => minska, annars öka
	if has_429 or err_rate > 0.1 or avg_lat > 1.2:
		new_rps = max(1, int(RATE_LIMIT_RPS * 0.7))
		if new_rps != RATE_LIMIT_RPS:
			RATE_LIMIT_RPS = new_rps
			with progress_lock:
				print(f"\n[auto] Sänker rate-limit till {RATE_LIMIT_RPS} rps (err={err_rate:.2f}, avg_lat={avg_lat:.2f}s)")
		# minska sid-workers något vid hög latens
		if MAX_PAGE_WORKERS > 4:
			MAX_PAGE_WORKERS = max(4, MAX_PAGE_WORKERS - 1)
			with progress_lock:
				print(f"[auto] Minskar sid-workers till {MAX_PAGE_WORKERS}")
	elif err_rate < 0.02 and avg_lat < 0.6:
		new_rps = min(100, RATE_LIMIT_RPS + 5)
		if new_rps != RATE_LIMIT_RPS:
			RATE_LIMIT_RPS = new_rps
			with progress_lock:
				print(f"\n[auto] Höjer rate-limit till {RATE_LIMIT_RPS} rps (err={err_rate:.2f}, avg_lat={avg_lat:.2f}s)")
		# öka sid-workers försiktigt
		if MAX_PAGE_WORKERS < 16:
			MAX_PAGE_WORKERS = min(16, MAX_PAGE_WORKERS + 1)
			with progress_lock:
				print(f"[auto] Ökar sid-workers till {MAX_PAGE_WORKERS}")

def init_db():
	# Skapar tabell för att hålla koll på redan nedladdade nummer
	with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
		conn.execute("PRAGMA journal_mode=WAL;")
		conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
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
		CREATE TABLE IF NOT EXISTS presets (
			name TEXT PRIMARY KEY,
			payload_json TEXT NOT NULL,
			created_at TEXT NOT NULL
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
	with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
		conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
		cur = conn.execute("""
			SELECT 1 FROM issues_downloads
			WHERE publication_code=? AND issue_code=? AND status IN ('downloaded','archived')
		""", (publication_code, issue_code))
		return cur.fetchone() is not None

def get_issue_status(publication_code, issue_code):
	with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
		conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
		cur = conn.execute("""
			SELECT status FROM issues_downloads
			WHERE publication_code=? AND issue_code=?
		""", (publication_code, issue_code))
		row = cur.fetchone()
		return row[0] if row else None

def mark_downloaded(publication_code, issue_code, issue_date, issue_name, filename):
	with db_write_lock:
		with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
			conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
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
	with db_write_lock:
		with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
			conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
			conn.execute("""
			UPDATE issues_downloads SET status='archived' WHERE publication_code=? AND issue_code=?
			""", (publication_code, issue_code))

def unmark_issue(publication_code, issue_code):
	with db_write_lock:
		with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
			conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
			conn.execute("""DELETE FROM issues_downloads WHERE publication_code=? AND issue_code=?""", (publication_code, issue_code))

def enqueue_job(job_type, publication_code, payload: dict):
	with db_write_lock:
		with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
			conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
			conn.execute("""
			INSERT INTO download_jobs (job_type, publication_code, payload_json, status, created_at)
			VALUES (?, ?, ?, 'queued', ?)
			""", (job_type, publication_code, json.dumps(payload), datetime.datetime.utcnow().isoformat()))

def list_jobs(status=None):
	with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
		conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
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
	with db_write_lock:
		with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
			conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
			if status:
				conn.execute("""DELETE FROM download_jobs WHERE status = ?""", (status,))
			else:
				conn.execute("""DELETE FROM download_jobs""")

def delete_jobs_by_ids(job_ids):
	if not job_ids:
		return
	with db_write_lock:
		with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
			conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
			qmarks = ",".join(["?"] * len(job_ids))
			conn.execute(f"DELETE FROM download_jobs WHERE id IN ({qmarks})", tuple(job_ids))

def update_job_payload(job_id, payload: dict):
	with db_write_lock:
		with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
			conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
			conn.execute("""
			UPDATE download_jobs SET payload_json=? WHERE id=?
			""", (json.dumps(payload), job_id))

def run_jobs(publications, *, skip_if_in_db=True, force=False):
	# Läs ut jobben i en separat, kort transaktion
	with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn_list:
		conn_list.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
		rows = conn_list.execute("""SELECT id, job_type, publication_code, payload_json FROM download_jobs WHERE status='queued' ORDER BY id ASC""").fetchall()

	for job_id, job_type, pub_code, payload_json in rows:
		started = datetime.datetime.utcnow().isoformat()
		with db_write_lock, sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn_upd:
			conn_upd.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
			conn_upd.execute("""UPDATE download_jobs SET status='running', started_at=? WHERE id=?""", (started, job_id))
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
			elif job_type == "all_issues":
				issues_info = getIssuesForPublication(pub_code, publications)
				if issues_info:
					downloadIssuesSubset(pub_code, publications, issues_info, skip_if_in_db=skip_if_in_db, force=force)
			else:
				raise ValueError(f"Okänt job_type: {job_type}")
			finished = datetime.datetime.utcnow().isoformat()
			with db_write_lock, sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn_done:
				conn_done.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
				conn_done.execute("""UPDATE download_jobs SET status='done', finished_at=?, last_error=NULL WHERE id=?""", (finished, job_id))
		except Exception as e:
			finished = datetime.datetime.utcnow().isoformat()
			with db_write_lock, sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn_fail:
				conn_fail.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
				conn_fail.execute("""UPDATE download_jobs SET status='failed', finished_at=?, last_error=? WHERE id=?""", (finished, str(e), job_id))

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

	response = http_post_json(url, payload, headers)
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

def compute_issues_for_job(job_type, pub_code, payload, publications):
	# Returnerar lista med (issueId, issueDate, issueName) för ett jobb
	if job_type == "latest_n":
		n = int(payload.get("n", 1))
		return getIssuesForPublication(pub_code, publications)[:max(0, n)]
	if job_type == "range_indices":
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
		return selected_info
	if job_type == "date_range":
		date_from = payload.get("from")
		date_to = payload.get("to")
		return filterIssuesByDateRange(pub_code, publications, date_from, date_to)
	if job_type == "all_issues":
		return getIssuesForPublication(pub_code, publications)
	return []

def count_downloaded_issues(publicationId, publications):
	issues = getIssuesForPublication(publicationId, publications)
	downloaded = 0
	for issueId, _, _ in issues:
		if is_downloaded(publicationId, issueId):
			downloaded += 1
	return downloaded, len(issues)

def save_preset(name, publication_codes):
	with db_write_lock:
		with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
			conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
			conn.execute("""
			INSERT INTO presets (name, payload_json, created_at)
			VALUES (?, ?, ?)
			ON CONFLICT(name) DO UPDATE SET payload_json=excluded.payload_json, created_at=excluded.created_at
			""", (name, json.dumps({"publication_codes": publication_codes}), datetime.datetime.utcnow().isoformat()))

def list_presets():
	with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
		conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
		cur = conn.execute("""SELECT name, payload_json, created_at FROM presets ORDER BY name ASC""")
		return cur.fetchall()

def delete_preset(name):
	with db_write_lock:
		with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
			conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
			conn.execute("""DELETE FROM presets WHERE name=?""", (name,))

def get_preset(name):
	with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
		conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
		cur = conn.execute("""SELECT payload_json FROM presets WHERE name=?""", (name,))
		row = cur.fetchone()
		return json.loads(row[0]) if row else None

def export_presets_to_file(target_path):
	with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
		conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
		cur = conn.execute("""SELECT name, payload_json, created_at FROM presets ORDER BY name ASC""")
		items = [{"name": n, "payload": json.loads(pj), "created_at": created} for (n, pj, created) in cur.fetchall()]
	data = {"version": 1, "exported_at": datetime.datetime.utcnow().isoformat(), "presets": items}
	with open(target_path, "w", encoding="utf-8") as f:
		json.dump(data, f, ensure_ascii=False, indent=2)

def import_presets_from_file(source_path, overwrite=False):
	with open(source_path, "r", encoding="utf-8") as f:
		data = json.load(f)
	items = data.get("presets", [])
	with db_write_lock:
		with sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn:
			conn.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
			if overwrite:
				conn.execute("DELETE FROM presets")
			for item in items:
				name = item.get("name")
				payload = item.get("payload") or {}
				created = item.get("created_at") or datetime.datetime.utcnow().isoformat()
				if not name:
					continue
				conn.execute("""
				INSERT INTO presets (name, payload_json, created_at)
				VALUES (?, ?, ?)
				ON CONFLICT(name) DO UPDATE SET payload_json=excluded.payload_json, created_at=excluded.created_at
				""", (name, json.dumps(payload), created))

def parse_index_spec(spec, max_index):
	parts = [p.strip() for p in spec.split(",") if p.strip()]
	indices = set()
	for part in parts:
		if "-" in part:
			a, b = part.split("-", 1)
			a = int(a); b = int(b)
			if a > b:
				a, b = b, a
			for i in range(max(1, a), min(max_index, b) + 1):
				indices.add(i)
		else:
			indices.add(int(part))
	return sorted(i for i in indices if 1 <= i <= max_index)

def getIssuePDFs(publicationId, issueId):
	url = f"https://reader.flipp.se/html5/reader/get_page_groups_from_eid.aspx?pubid={publicationId}&eid={issueId}"
	# No auth! :)
	response = http_get_json(url)
	pdf_urls = [page["pdf"] for group in response["pageGroups"] for page in group["pages"]]
	return pdf_urls


def readPdf(pdf):
	rate_limited_sleep()
	t0 = time.time()
	req = http_session.get(url=pdf, timeout=REQUEST_TIMEOUT_SECS)
	if req.ok:
		t1 = time.time()
		record_http_result(True, t1 - t0, req.status_code)
		return io.BytesIO(req.content)
	record_http_result(False, time.time() - t0, req.status_code if req is not None else 0)
	raise Exception(f"Error Code:  {req.status_code if req is not None else 'N/A'}")

def download_pdfs_concurrently(urls, max_workers, progress_prefix=None, print_mode="inline"):
	# Laddar ner alla urls parallellt och returnerar en lista av BytesIO i samma ordning
	if not urls:
		return []
	from concurrent.futures import ThreadPoolExecutor, as_completed
	results = [None] * len(urls)  # (BytesIO, size_bytes)
	def fetch(idx, url):
		rate_limited_sleep()
		t0 = time.time()
		resp = http_session.get(url=url, timeout=REQUEST_TIMEOUT_SECS)
		if not resp.ok:
			record_http_result(False, time.time() - t0, resp.status_code)
			raise Exception(f"HTTP {resp.status_code} for {url}")
		content = resp.content
		record_http_result(True, time.time() - t0, resp.status_code)
		return idx, (io.BytesIO(content), len(content))
	with ThreadPoolExecutor(max_workers=max_workers) as executor:
		fut_to_idx = {executor.submit(fetch, i, url): i for i, url in enumerate(urls)}
		completed = 0
		total = len(urls)
		start_ts = time.time()
		total_bytes = 0
		if progress_prefix and print_mode == "inline":
			render_progress(progress_prefix, 0, total, start_ts=start_ts, bytes_done=0)
		for fut in as_completed(fut_to_idx):
			i, pair = fut.result()
			results[i] = pair
			total_bytes += pair[1]
			if progress_prefix and print_mode == "inline":
				completed += 1
				render_progress(progress_prefix, completed, total, start_ts=start_ts, bytes_done=total_bytes)
			elif progress_prefix and print_mode == "lines" and SHOW_PROGRESS:
				completed += 1
				# skriv bara var 5:e sida eller sista, för mindre overhead
				if (completed % 5 == 0) or completed == total:
					elapsed = max(0.0, time.time() - start_ts)
					items_per_s = completed / elapsed if elapsed > 0 else 0.0
					eta = (total - completed) / items_per_s if items_per_s > 0 else 0
					mm = int(eta // 60); ss = int(eta % 60)
					mbps = (total_bytes / 1048576.0) / elapsed if elapsed > 0 else 0.0
					with progress_lock:
						print(f"{progress_prefix} {completed}/{total} • {items_per_s:.2f}/s {mbps:.2f} MB/s ETA {mm:02d}:{ss:02d}")
	return results

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
		return (False, 0)
	
	merger = PdfMerger()
	if MAX_PAGE_WORKERS and MAX_PAGE_WORKERS > 1:
		progress_prefix = f"Sidor: {issueName}"
		print_mode = "inline" if (MAX_ISSUE_WORKERS <= 1) else "lines"
		buffers = download_pdfs_concurrently(pdfs, MAX_PAGE_WORKERS, progress_prefix=progress_prefix, print_mode=print_mode)
		total_bytes = 0
		for bio, size in buffers:
			total_bytes += size
			merger.append(PdfReader(bio))
	else:
		total_bytes = 0
	for pdf in pdfs:
			bio = readPdf(pdf)
			total_bytes += len(bio.getbuffer())
			merger.append(PdfReader(bio))

	if not os.path.exists(outputFolder):
		os.makedirs(outputFolder, exist_ok=True)
	
	# Skriv atomiskt via tempfil
	temp_dir = os.path.join(outputFolder, ".tmp")
	if not os.path.exists(temp_dir):
		os.makedirs(temp_dir, exist_ok=True)
	temp_file = os.path.join(temp_dir, issueName + ".part")
	merger.write(temp_file)
	merger.close()
	# Verifiera
	try:
		_ = PdfReader(temp_file)
	except Exception:
		try:
			os.remove(temp_file)
		except Exception:
			pass
		raise Exception("Verifiering av PDF misslyckades")
	# Atomiskt byte
	os.replace(temp_file, outputFile)
	return (True, total_bytes)


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
			ok, _bytes = writePdf(getIssuePDFs(publicationId, issue), name, filename)
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
	# Parallellisera nedladdning av flera nummer om möjligt
	if MAX_ISSUE_WORKERS and MAX_ISSUE_WORKERS > 1:
		downloadIssuesSubsetConcurrent(publicationId, publications, selected, skip_if_in_db=skip_if_in_db, force=force, max_workers=MAX_ISSUE_WORKERS)
		return
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
		ok, _bytes = writePdf(getIssuePDFs(publicationId, issueId), name, filename)
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
		ok, _bytes = writePdf(getIssuePDFs(publicationId, issueId), name, filename)
		if ok:
			mark_downloaded(publicationId, issueId, issueDate, issueName, filename)
			print(f"Written file: {filename}")
		print()

def downloadIssuesSubsetConcurrent(publicationId, publications, issues_info_subset, *, skip_if_in_db=True, force=False, max_workers=3):
	if not issues_info_subset:
		return
	from concurrent.futures import ThreadPoolExecutor, as_completed
	name = getPublicationNameFromId(publicationId, publications)
	publicationFolder = os.path.join(OUTPUTPATH, safeName(name))

	def worker(issueId, issueDate, issueName):
		filename = safeName(f"{name} - {issueDate} - {issueName}.pdf")
		msgs = []
		msgs.append(f"Downloading: {issueId} - {name} - ({issueDate}, {issueName})")
		if skip_if_in_db and not force and is_downloaded(publicationId, issueId):
			return "\n".join(msgs + ["Already downloaded (DB)"])
		if not force and os.path.isfile(os.path.join(publicationFolder, filename)):
			if skip_if_in_db and not is_downloaded(publicationId, issueId):
				mark_downloaded(publicationId, issueId, issueDate, issueName, filename)
			return "\n".join(msgs + ["File already exists"])
		ok, _bytes = writePdf(getIssuePDFs(publicationId, issueId), name, filename)
		if ok:
			mark_downloaded(publicationId, issueId, issueDate, issueName, filename)
			return "\n".join(msgs + [f"Written file: {filename}"])
		return "\n".join(msgs + ["Skipped writing (already exists)"])

	with ThreadPoolExecutor(max_workers=max_workers) as executor:
		futures = [executor.submit(worker, issueId, issueDate, issueName) for (issueId, issueDate, issueName) in issues_info_subset]
		total = len(futures)
		completed = 0
		if SHOW_PROGRESS:
			render_progress(f"Nummer: {name}", 0, total)
		for fut in as_completed(futures):
			try:
				# ticka först så progress inte släpar efter logg
				if SHOW_PROGRESS:
					completed += 1
					render_progress(f"Nummer: {name}", completed, total)
				msg = fut.result()
				with progress_lock:
					print("\n" + msg)
			except Exception as e:
				with progress_lock:
					print(f"\nError in worker: {e}")


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
	parser.add_argument("--workers-pages", type=int, help="Antal trådar för sidnedladdning per nummer (default 6).")
	parser.add_argument("--workers-issues", type=int, help="Antal trådar för parallella nummer (default 3).")
	parser.add_argument("--no-progress", action="store_true", help="Stäng av progress bars under nedladdning.")
	parser.add_argument("--rate-limit", type=int, help="Max antal HTTP-anrop per sekund (0=av).")
	parser.add_argument("--timeout", type=int, help="HTTP-timeout i sekunder (default 15).")
	parser.add_argument("--export-presets", help="Exportera alla presets till JSON-fil.")
	parser.add_argument("--import-presets", help="Importera presets från JSON-fil.")
	parser.add_argument("--import-overwrite", action="store_true", help="Rensa befintliga presets före import.")
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
			print("\nVälj åtgärd: [1] Kör kö [2] Töm kö [3] Ta bort valda [4] Ändra jobb [5] Markera valda som nedladdade/arkiverade [6] Avmarkera (inte nedladdade) [0] Tillbaka")
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
			elif choice == "5":
				if not rows:
					continue
				val = input("Vilka jobb vill du markera som nedladdade? (t.ex. 1,3-5): ").strip()
				arch = input("Ska dessa markeras som archived istället för downloaded? [j/N]: ").strip().lower() in ("j","y","yes")
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
				done = 0
				for i in sorted(sel):
					if not (1 <= i <= len(rows)):
						continue
					job_id, job_type, pub_code, payload_json, status, created_at = rows[i-1]
					try:
						payload = json.loads(payload_json or "{}")
					except Exception:
						payload = {}
					issues = compute_issues_for_job(job_type, pub_code, payload, publications)
					pub_name = getPublicationNameFromId(pub_code, publications) or pub_code
					for issueId, issueDate, issueName in issues:
						filename = safeName(f"{pub_name} - {issueDate} - {issueName}.pdf")
						if arch:
							mark_downloaded(pub_code, issueId, issueDate, issueName, filename)  # ensure row exists
							mark_archived(pub_code, issueId)
						else:
							mark_downloaded(pub_code, issueId, issueDate, issueName, filename)
					# Markera jobbet som klart
					with db_write_lock, sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT_SECS) as conn_mark:
						conn_mark.execute(f"PRAGMA busy_timeout={DB_TIMEOUT_SECS*1000};")
						conn_mark.execute("""UPDATE download_jobs SET status='done', finished_at=?, last_error=NULL WHERE id=?""", (datetime.datetime.utcnow().isoformat(), job_id))
					done += 1
				print(f"Markerade {done} jobb och deras nummer som nedladdade i DB.")
			elif choice == "6":
				if not rows:
					continue
				val = input("Vilka jobb vill du avmarkera (ta bort DB-markering)? (t.ex. 1,3-5): ").strip()
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
				done = 0
				for i in sorted(sel):
					if not (1 <= i <= len(rows)):
						continue
					job_id, job_type, pub_code, payload_json, status, created_at = rows[i-1]
					try:
						payload = json.loads(payload_json or "{}")
					except Exception:
						payload = {}
					issues = compute_issues_for_job(job_type, pub_code, payload, publications)
					for issueId, _, _ in issues:
						unmark_issue(pub_code, issueId)
					done += 1
				print(f"Tog bort DB-markering för {done} jobb (alla tillhörande nummer).")
			else:
				print("Ogiltigt val.")

	args = parse_args()

	# Token via arg eller env
	token = args.token or os.environ.get("FLIPP_TOKEN", "")
	if not token:
		print("Error: Token saknas. Ange --token eller sätt FLIPP_TOKEN i miljön.")
		sys.exit(1)

	# Logging
	log_dir = os.path.join(os.getcwd(), "logs")
	try:
		os.makedirs(log_dir, exist_ok=True)
	except Exception:
		pass
	log_path = os.path.join(log_dir, "flipp-dl.log")
	logging.basicConfig(filename=log_path, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
	logging.info("Startar flipp-dl")

	# Output path
	if args.output:
		OUTPUTPATH = args.output

	# Workers (konkurrens)
	global MAX_PAGE_WORKERS, MAX_ISSUE_WORKERS, RATE_LIMIT_RPS, REQUEST_TIMEOUT_SECS
	if args.workers_pages is not None and args.workers_pages > 0:
		MAX_PAGE_WORKERS = args.workers_pages
	if args.workers_issues is not None and args.workers_issues > 0:
		MAX_ISSUE_WORKERS = args.workers_issues
	if args.rate_limit is not None and args.rate_limit >= 0:
		RATE_LIMIT_RPS = args.rate_limit
	if args.timeout is not None and args.timeout > 0:
		REQUEST_TIMEOUT_SECS = args.timeout

	# Progress
	global SHOW_PROGRESS
	if args.no_progress:
		SHOW_PROGRESS = False

	# Init DB
	init_db()

	# HTTP session
	setup_http_session()

	publicationJson = getPublicationsJSON(token)
	plist = getPublicationsInfo(publicationJson)

	# Presets export/import (icke-interaktivt)
	if args.export_presets:
		try:
			export_presets_to_file(args.export_presets)
			print(f"Exporterade presets till {args.export_presets}")
		except Exception as e:
			print(f"Fel vid export: {e}")
		return
	if args.import_presets:
		try:
			import_presets_from_file(args.import_presets, overwrite=args.import_overwrite)
			print(f"Importerade presets från {args.import_presets}{' (overwrite)' if args.import_overwrite else ''}")
		except Exception as e:
			print(f"Fel vid import: {e}")
		return

	# Jobbkö-hantering (icke-interaktivt)
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
			print(f"[3] Inställningar (workers/progress)  [pages={MAX_PAGE_WORKERS}, issues={MAX_ISSUE_WORKERS}, progress={'på' if SHOW_PROGRESS else 'av'}, rps={RATE_LIMIT_RPS}, timeout={REQUEST_TIMEOUT_SECS}s]")
			print("[0] Avsluta")
			root = input("Ditt val: ").strip()
			if root in ("0",""):
				return
			if root == "2":
				rows = list_jobs()
				if not rows or not any((row[4] == 'queued') for row in rows):
					print("Inga köade jobb. Återgår till huvudmeny.")
				else:
					interactive_queue_manager(publicationJson)
				continue
			if root == "3":
				def interactive_settings():
					global MAX_PAGE_WORKERS, MAX_ISSUE_WORKERS, SHOW_PROGRESS, RATE_LIMIT_RPS, REQUEST_TIMEOUT_SECS
					while True:
						print("\n--- Inställningar ---")
						print(f"[1] Sätt workers för sidor (nu: {MAX_PAGE_WORKERS})")
						print(f"[2] Sätt workers för nummer (nu: {MAX_ISSUE_WORKERS})")
						print(f"[3] Växla progress bars (nu: {'på' if SHOW_PROGRESS else 'av'})")
						print(f"[4] Sätt rate-limit RPS (nu: {RATE_LIMIT_RPS}, 0=av)")
						print(f"[5] Sätt HTTP-timeout (nu: {REQUEST_TIMEOUT_SECS}s)")
						print("[0] Tillbaka")
						opt = input("Ditt val: ").strip()
						if opt in ("0",""):
							return
						if opt == "1":
							val = input("Nytt värde (>=1): ").strip()
							try:
								n = int(val)
								if n >= 1:
									MAX_PAGE_WORKERS = n
									print(f"Satte sid-workers till {n}.")
								else:
									print("Ange ett tal >= 1.")
							except Exception:
								print("Ogiltigt tal.")
						elif opt == "2":
							val = input("Nytt värde (>=1): ").strip()
							try:
								n = int(val)
								if n >= 1:
									MAX_ISSUE_WORKERS = n
									print(f"Satte nummer-workers till {n}.")
								else:
									print("Ange ett tal >= 1.")
							except Exception:
								print("Ogiltigt tal.")
						elif opt == "3":
							SHOW_PROGRESS = not SHOW_PROGRESS
							print(f"Progress bars är nu {'på' if SHOW_PROGRESS else 'av'}.")
						elif opt == "4":
							val = input("Nytt värde (>=0, 0=av): ").strip()
							try:
								n = int(val)
								if n >= 0:
									RATE_LIMIT_RPS = n
									print(f"Satte rate-limit till {n} rps.")
								else:
									print("Ange ett tal >= 0.")
							except Exception:
								print("Ogiltigt tal.")
						elif opt == "5":
							val = input("Nytt timeout (sek, >0): ").strip()
							try:
								n = int(val)
								if n > 0:
									REQUEST_TIMEOUT_SECS = n
									print(f"Satte timeout till {n}s.")
								else:
									print("Ange ett tal > 0.")
							except Exception:
								print("Ogiltigt tal.")
						else:
							print("Ogiltigt val.")
				interactive_settings()
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
				print("\n[0] Till huvudmeny    [P] Presets")
				val = input("Ditt val: ").strip()
				if val in ("0",""):
					break
				if val.lower() == "p":
					# Preset manager
					while True:
						print("\n--- Presets ---")
						pres = list_presets()
						if not pres:
							print("Inga presets sparade.")
						else:
							for i, (n, _p, created) in enumerate(pres, start=1):
								print(f"[{i}] {n} ({created})")
						print("\nVälj: [nummer] kör åtgärd, [d] radera, [e] exportera, [i] importera, [0] tillbaka")
						choice = input("Ditt val: ").strip().lower()
						if choice in ("0",""):
							break
						if choice == "d":
							name = input("Namn på preset att radera: ").strip()
							if name:
								delete_preset(name)
								print("Raderad (om den fanns).")
							continue
						if choice == "e":
							path = input("Sökväg för export (t.ex. presets.json): ").strip()
							if path:
								try:
									export_presets_to_file(path)
									print(f"Exporterade till {path}.")
								except Exception as e:
									print(f"Fel vid export: {e}")
							continue
						if choice == "i":
							path = input("Sökväg för import (t.ex. presets.json): ").strip()
							if path:
								ow = input("Skriv över befintliga presets? [j/N]: ").strip().lower() in ("j","y","yes")
								try:
									import_presets_from_file(path, overwrite=ow)
									print(f"Importerade från {path}{' (overwrite)' if ow else ''}.")
								except Exception as e:
									print(f"Fel vid import: {e}")
							continue
						if choice.isdigit():
							i = int(choice)
							if 1 <= i <= len(pres):
								name, payload_json, _created = pres[i-1]
								payload = json.loads(payload_json)
								pcodes = payload.get("publication_codes", [])
								if not pcodes:
									print("Preset saknar publikationer."); continue
								print(f"\nPreset '{name}' ({len(pcodes)} publikationer)")
								print("[1] Lägg i kö: senaste N\n[2] Lägg i kö: alla nummer\n[3] Ladda ner alla nu\n[0] Tillbaka")
								actp = input("Ditt val: ").strip()
								if actp == "1":
									n_str = input("Hur många senaste nummer? (t.ex. 1): ").strip()
									try:
										n = int(n_str) if n_str else 1
									except Exception:
										print("Ogiltigt tal."); continue
									for code in pcodes:
										enqueue_job("latest_n", code, {"n": n})
									print(f"Lade till {len(pcodes)} jobb i kön.")
								elif actp == "2":
									for code in pcodes:
										enqueue_job("all_issues", code, {})
									print(f"Lade till {len(pcodes)} jobb (alla nummer) i kön.")
								elif actp == "3":
									for code in pcodes:
										issues_info = getIssuesForPublication(code, publicationJson)
										downloadIssuesSubsetConcurrent(code, publicationJson, issues_info, skip_if_in_db=True, force=False, max_workers=MAX_ISSUE_WORKERS)
									print("Klart.")
								else:
									continue
							continue
						print("Ogiltigt val.")
					continue
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
						dl, total = count_downloaded_issues(pcode, publicationJson)
						print(f"[{idx}] {pname} ({num_issues} nummer)  –  DB: {dl}/{total}")
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
							print("\nVälj åtgärd:\n[1] Lista nummer\n[2] Ladda ner senaste N\n[3] Lägg till i kö: senaste N\n[4] Lägg till i kö: indexintervall\n[5] Lägg till i kö: datumintervall\n[6] Hantera kö\n[9] Ladda ner alla nummer\n[10] Lägg till i kö: alla nummer\n[7] Till kategorier\n[8] Till huvudmeny\n[0] Till publikationer")
							act = input("Ditt val: ").strip()
							# Snabbkommandon: d/a/c <indexspec>, l <indexspec>
							if re.match(r"^[dac]\s+[\d,\-\s]+$", act, re.IGNORECASE):
								if not issues_info:
									issues_info = getIssuesForPublication(selected_codes[0], publicationJson)
								cmd = act.split(None, 1)[0].lower()
								spec = act[len(cmd):].strip()
								idxs = parse_index_spec(spec, len(issues_info))
								name_pub = getPublicationNameFromId(selected_codes[0], publicationJson) or ""
								for i in idxs:
									issueId, issueDate, issueName = issues_info[i-1]
									filename = safeName(f"{name_pub} - {issueDate} - {issueName}.pdf")
									if cmd == "d":
										mark_downloaded(selected_codes[0], issueId, issueDate, issueName, filename)
									elif cmd == "a":
										mark_downloaded(selected_codes[0], issueId, issueDate, issueName, filename)
										mark_archived(selected_codes[0], issueId)
									else:
										unmark_issue(selected_codes[0], issueId)
								print(f"Uppdaterade {len(idxs)} nummer."); continue
							if re.match(r"^l\s+[\d,\-\s]+$", act, re.IGNORECASE):
								if not issues_info:
                                    # hämta vid behov
									issues_info = getIssuesForPublication(selected_codes[0], publicationJson)
								spec = act.split(None, 1)[1].strip()
								idxs = parse_index_spec(spec, len(issues_info))
								subset = [issues_info[i-1] for i in idxs]
								downloadIssuesSubsetConcurrent(selected_codes[0], publicationJson, subset, skip_if_in_db=True, force=False, max_workers=MAX_ISSUE_WORKERS)
								print("Klart."); continue
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
								for idx, (iid, idate, iname) in enumerate(issues_info, start=1):
									st = get_issue_status(selected_codes[0], iid)
									flag = "✓" if st == "downloaded" else ("A" if st == "archived" else "–")
									print(f"[{idx}] {idate} - {iname}  [{flag}]")
								print("\nVill du ändra status på enskilda nummer? [j/N]")
								ans = input().strip().lower()
								if ans in ("j","y","yes"):
									mode = input("Välj status [d=downloaded, a=archived, c=clear]: ").strip().lower()
									if mode not in ("d","a","c"):
										print("Ogiltigt val."); continue
									span = input("Vilka index? (t.ex. 1,3-5): ").strip()
									if not span:
										print("Inget urval angivet."); continue
									parts = [p.strip() for p in span.split(",") if p.strip()]
									indices = set()
									ok = True
									try:
										for part in parts:
											if "-" in part:
												a, b = part.split("-", 1); a = int(a); b = int(b)
												if a > b: a, b = b, a
												for x in range(a, b+1):
													indices.add(x)
											else:
												indices.add(int(part))
									except Exception:
										ok = False
									if not ok:
										print("Ogiltigt urval."); continue
									name_pub = getPublicationNameFromId(selected_codes[0], publicationJson) or ""
									changed = 0
									for i in sorted(indices):
										if 1 <= i <= len(issues_info):
											issueId, issueDate, issueName = issues_info[i-1]
											filename = safeName(f"{name_pub} - {issueDate} - {issueName}.pdf")
											if mode == "d":
												mark_downloaded(selected_codes[0], issueId, issueDate, issueName, filename)
											elif mode == "a":
												mark_downloaded(selected_codes[0], issueId, issueDate, issueName, filename)
												mark_archived(selected_codes[0], issueId)
											else:
												unmark_issue(selected_codes[0], issueId)
											changed += 1
									print(f"Uppdaterade {changed} nummer.")
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
							if act == "9":
								issues_info = getIssuesForPublication(selected_codes[0], publicationJson)
								if not issues_info:
									print("Inga nummer hittades."); continue
								downloadIssuesSubset(selected_codes[0], publicationJson, issues_info, skip_if_in_db=True, force=False)
								print("Klart."); continue
							if act == "10":
								enqueue_job("all_issues", selected_codes[0], {})
								print("Jobb tillagt (alla nummer)."); continue
							print("Ogiltigt val.")
					else:
						while True:
							print("\nValda publikationer:")
							for code in selected_codes:
								name = getPublicationNameFromId(code, publicationJson) or code
								print(f"- {name} ({code})")
							print("Välj åtgärd för ALLA valda:\n[1] Lägg i kö: senaste N\n[2] Lägg i kö: indexintervall\n[3] Lägg i kö: datumintervall\n[4] Hantera kö nu\n[5] Lägg i kö: alla nummer\n[6] Ladda ner alla nummer nu\n[7] Spara urval som preset\n[0] Till publikationer\n[H] Huvudmeny")
							act = input("Ditt val: ").strip()
							if act in ("0",""): break
							if act.lower() == "h": go_main = True; break
							# Snabbkommandon: n <N>, all, dlall, save <name>
							if re.match(r"^n\s+\d+$", act, re.IGNORECASE):
								n = int(act.split()[1])
								for code in selected_codes:
									enqueue_job("latest_n", code, {"n": n})
								print(f"Lade till {len(selected_codes)} jobb i kön."); continue
							if act.strip().lower() == "all":
								for code in selected_codes:
									enqueue_job("all_issues", code, {})
								print(f"Lade till {len(selected_codes)} jobb (alla nummer) i kön."); continue
							if act.strip().lower() == "dlall":
								for code in selected_codes:
									issues_info = getIssuesForPublication(code, publicationJson)
									downloadIssuesSubsetConcurrent(code, publicationJson, issues_info, skip_if_in_db=True, force=False, max_workers=MAX_ISSUE_WORKERS)
								print("Klart."); continue
							if re.match(r"^save\s+.+$", act, re.IGNORECASE):
								name = act.split(None, 1)[1].strip()
								save_preset(name, selected_codes)
								print(f"Sparade preset '{name}' ({len(selected_codes)} publikationer)."); continue
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
							if act == "5":
								for code in selected_codes:
									enqueue_job("all_issues", code, {})
								print(f"Lade till {len(selected_codes)} jobb (alla nummer) i kön."); continue
							if act == "7":
								name = input("Namn på preset: ").strip()
								if name:
									save_preset(name, selected_codes)
									print(f"Sparade preset '{name}' ({len(selected_codes)} publikationer).")
								continue
							if act == "6":
								for code in selected_codes:
									issues_info = getIssuesForPublication(code, publicationJson)
									downloadIssuesSubsetConcurrent(code, publicationJson, issues_info, skip_if_in_db=True, force=False, max_workers=MAX_ISSUE_WORKERS)
								print("Klart.")
								continue
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
