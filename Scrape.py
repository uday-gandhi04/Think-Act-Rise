#!/usr/bin/env python3
"""
ecourts_causelist_checker.py

Purpose:
 - Given CNR or CaseType+Number+Year, check if that case is listed today or tomorrow
 - If listed: show serial number and court name
 - Optionally download cause list PDF (or the specific court's PDF)
 - Save output to JSON

Modes:
 - API mode (preferred): requires ECOURTS_API_KEY env var and an eCourts API endpoint.
 - Selenium interactive mode: opens Chrome, you manually solve CAPTCHA, then the script extracts the listing.

Dependencies:
 - pip install requests beautifulsoup4 selenium python-dateutil
 - Chrome/Chromium and matching chromedriver must be installed for Selenium mode.
   (or use webdriver-manager to auto-download chromedriver)
 - Set ECOURTS_API_KEY environment variable if you have API access.

Usage examples:
  # Selenium interactive mode (default)
  python ecourts_causelist_checker.py --today --cnr MHAU012345662020 --court-complex "Patiala House Court Complex" --court "District and Sessions Judge,New Delhi, PHC"

  # API mode (if you have API key and endpoint configured in code)
  ECOURTS_API_KEY=your_key python ecourts_causelist_checker.py --tomorrow --case "CC" 123 2023 --out result.json

"""

import os
import re
import json
import argparse
import datetime
from dateutil import tz, relativedelta
from bs4 import BeautifulSoup

# network libs
import requests

# selenium imports (interactive fallback)
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
import time
import base64

# ---------- Helper functions ----------

def iso_date_for(which='today'):
    tz_ind = tz.gettz('Asia/Kolkata')
    now = datetime.datetime.now(tz=tz_ind).date()
    if which == 'today':
        return now.strftime('%Y-%m-%d')
    elif which == 'tomorrow':
        return (now + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    else:
        return which

def save_json(obj, path):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    print(f"[+] Saved JSON to {path}")

# ---------- API mode (sketch / placeholder) ----------
# NOTE: official eCourts API requires auth. If you have a key, set env var ECOURTS_API_KEY.
# The real endpoint and parameters should be obtained from the eCourts API docs / your admin.
def api_get_cause_list_by_params(api_key, state_code, district_code, complex_code, court_code, date_iso, jurisdiction='district'):
    """
    Placeholder function showing how to call a cause-list API if you have API access.
    The actual endpoint and parameter names may differ depending on the API version/host.
    """
    # Example placeholder endpoint (replace with your API endpoint from eCourts docs)
    endpoint = "https://apis.ecourts.gov.in/eciapi/17/district-court/cause-list"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    payload = {
        "state_code": state_code,
        "district_code": district_code,
        "court_complex": complex_code,
        "court_code": court_code,
        "cause_list_date": date_iso
    }
    resp = requests.post(endpoint, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()

# ---------- Selenium interactive fallback ----------
def selenium_fetch_cause_list_interactive(
        target_date_iso='today',
        court_complex=None,
        court_name=None,
        civ_or_crim='Civil',
        download_pdf=False,
        headless=False
    ):
    """
    This function:
     - opens the district cause list page (eCourts / local district page)
     - you manually solve the CAPTCHA presented (script pauses and prompts)
     - after you press Enter, it clicks the Civil/Criminal button and waits for result
     - it tries to extract the displayed cause list HTML/text
     - optionally, save PDF via Page.printToPDF (Chrome DevTools)
    Returns a dict with extracted text and optionally base64 pdf bytes (if available)
    """
    # Choose the site (example: newdelhi.dcourts)
    target_url = "https://newdelhi.dcourts.gov.in/cause-list-%E2%81%84-daily-board/"  # user-specified site
    chrome_opts = Options()
    if headless:
        chrome_opts.add_argument("--headless=new")  # modern headless
    chrome_opts.add_argument("--disable-gpu")
    chrome_opts.add_argument("--no-sandbox")
    chrome_opts.add_argument("--window-size=1200,900")

    driver = webdriver.Chrome(options=chrome_opts)
    driver.get(target_url)
    print("[i] Browser opened. You must now interactively select options on the page and solve the CAPTCHA.")
    print("    - Select Court Complex / Court Establishment / Court Number and Date")
    print("    - Enter the CAPTCHA on the page and click the 'Civil' or 'Criminal' button to display cause list")
    input("After you've solved the CAPTCHA and the cause list for your court/date is visible in the browser, press Enter here to continue...")

    # Wait a little for the page to fully render the cause list (adjust as needed)
    time.sleep(2)

    # Attempt to find a PDF viewer or cause list container
    page_html = driver.page_source
    soup = BeautifulSoup(page_html, 'html.parser')

    # Try to find text-based cause list (the site sometimes prints HTML)
    text_content = soup.get_text(separator="\n")
    result = {
        "raw_html": str(soup),
        "raw_text": text_content
    }

    # Optionally attempt to save PDF using Chrome CDP printToPDF
    pdf_b64 = None
    if download_pdf:
        try:
            # Use CDP to print page as PDF
            print("[i] Attempting to print page to PDF via Chrome DevTools protocol...")
            # selenium 4 has execute_cdp_cmd
            print_options = {
                "landscape": False,
                "displayHeaderFooter": False,
                "printBackground": True,
                "preferCSSPageSize": True
            }
            pdf_b64 = driver.execute_cdp_cmd("Page.printToPDF", print_options).get("data")
            if pdf_b64:
                result["cause_list_pdf_b64"] = pdf_b64
            else:
                print("[!] PDF generation returned no data.")
        except Exception as e:
            print("[!] PDF generation failed:", e)

    driver.quit()
    return result

# ---------- Search helpers ----------
def search_case_in_text_by_cnr(text, cnr):
    """
    Searches the cause-list text for the CNR string. Returns first match lines and a context block.
    """
    if not cnr:
        return None
    idx = text.find(cnr)
    if idx == -1:
        return None
    # find line and a few lines of context
    lines = text.splitlines()
    for i,ln in enumerate(lines):
        if cnr in ln:
            # try to guess serial/court info from surrounding lines
            context = "\n".join(lines[max(0,i-3):i+4])
            # naive serial extraction: search for nearby 'Serial' or digits
            m_serial = re.search(r'\bSerial\b[:\s]*([0-9]+)', context, re.IGNORECASE)
            serial = m_serial.group(1) if m_serial else None
            # naive court name: look for common court headings in context
            m_court = re.search(r'Court\s*[:\-]\s*([A-Za-z0-9 ,.-]+)', context)
            court = m_court.group(1).strip() if m_court else None
            return {"context": context, "line_no": i, "serial": serial, "court": court}
    return None

def search_case_by_parts(text, case_type, number, year):
    """Find occurrences of a case-type/number/year combination in text."""
    if not (case_type and number and year):
        return None
    # build common patterns: "CC NI ACT/10611/2022" or "CC NI ACT/10611/2" etc.
    patterns = [
        rf'\b{re.escape(case_type)}\b.*?{re.escape(str(number))}.*?{re.escape(str(year))}',
        rf'{re.escape(case_type)}[^\n]*{re.escape(str(number))}[^\n]*{re.escape(str(year))}',
        rf'\b{re.escape(str(number))}/{re.escape(str(year))}\b'
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            start = max(0, m.start()-200)
            stop = m.end()+200
            ctx = text[start:stop]
            # try to extract serial similarly
            m_serial = re.search(r'\bSerial\b[:\s]*([0-9]+)', ctx, re.IGNORECASE)
            serial = m_serial.group(1) if m_serial else None
            return {"context": ctx, "serial": serial}
    return None

# ---------- CLI / main ----------
def main():
    parser = argparse.ArgumentParser(description="Check eCourts cause-list for a case (today/tomorrow).")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument('--cnr', help='Full CNR string (16 chars)')
    g_case = g.add_argument_group('case')
    g.add_argument('--case', nargs=3, metavar=('TYPE','NUMBER','YEAR'), help='Case specified as TYPE NUMBER YEAR, e.g. "CC" 123 2022')
    parser.add_argument('--today', action='store_true', help='Check for today (default)')
    parser.add_argument('--tomorrow', action='store_true', help='Check for tomorrow')
    parser.add_argument('--court-complex', default=None, help='Court Complex name (for Selenium interactive mode)')
    parser.add_argument('--court', default=None, help='Specific court name / establishment (for Selenium interactive mode)')
    parser.add_argument('--causelist', action='store_true', help='Download entire cause list PDF (interactive mode)')
    parser.add_argument('--api', action='store_true', help='Try API mode (requires ECOURTS_API_KEY env var and valid API endpoint)')
    parser.add_argument('--out', default='ecourts_result.json', help='Output JSON filename')
    parser.add_argument('--headless', action='store_true', help='Run Chrome headless in Selenium mode (may be blocked by captcha)')
    args = parser.parse_args()

    # select date
    date_choice = 'today'
    if args.tomorrow:
        date_choice = 'tomorrow'
    elif args.today:
        date_choice = 'today'

    date_iso = iso_date_for(date_choice)

    # build query
    query = {}
    if args.cnr:
        query['cnr'] = args.cnr.strip()
    elif args.case:
        typ, num, yr = args.case
        query['case_type'] = typ.strip()
        query['case_number'] = num.strip()
        query['case_year'] = yr.strip()

    output = {
        "query": query,
        "checked_date": date_iso,
        "method": None,
        "found": False,
        "matches": [],
        "notes": []
    }

    # Try API mode if requested and API key is present
    if args.api:
        api_key = os.getenv('ECOURTS_API_KEY')
        if not api_key:
            print("[!] ECOURTS_API_KEY not set in environment; skipping API mode.")
            output['notes'].append("API mode requested but ECOURTS_API_KEY not set.")
        else:
            output['method'] = 'api'
            try:
                # !!! You must replace the parameters below with the official ones from your API docs !!!
                print("[i] Calling eCourts API (placeholder). Replace endpoint/params with official ones.")
                api_res = api_get_cause_list_by_params(api_key, state_code='09', district_code='13',
                                                      complex_code=args.court_complex or '', court_code=args.court or '',
                                                      date_iso=date_iso)
                # Example: api_res should contain cause list items you can search
                text_blob = json.dumps(api_res)  # convert to text for search
                if 'cnr' in query:
                    match = search_case_in_text_by_cnr(text_blob, query['cnr'])
                else:
                    match = search_case_by_parts(text_blob, query['case_type'], query['case_number'], query['case_year'])
                if match:
                    output['found'] = True
                    output['matches'].append(match)
                else:
                    output['found'] = False
                output['api_response_sample'] = api_res
            except Exception as e:
                output['notes'].append(f"API error: {e}")
                print("[!] API mode failed:", e)

    # If not found / not using API, fallback to interactive Selenium
    if not output.get('found'):
        output['method'] = output.get('method') or 'selenium_interactive'
        print("[i] Falling back to Selenium interactive mode. A Chrome window will open.")
        sres = selenium_fetch_cause_list_interactive(
            target_date_iso=date_iso,
            court_complex=args.court_complex,
            court_name=args.court,
            civ_or_crim='Civil',
            download_pdf=args.causelist,
            headless=args.headless
        )
        text_blob = sres.get('raw_text','')
        if 'cnr' in query:
            match = search_case_in_text_by_cnr(text_blob, query['cnr'])
        else:
            match = search_case_by_parts(text_blob, query.get('case_type'), query.get('case_number'), query.get('case_year'))

        if match:
            output['found'] = True
            output['matches'].append(match)
            print("[+] Case found. Context:")
            print(match['context'])
            if match.get('serial'): print("Serial:", match.get('serial'))
            if match.get('court'): print("Court:", match.get('court'))
        else:
            print("[!] Case not found in the displayed cause list content.")
            output['notes'].append("Case not found in the displayed cause list content.")
        # if pdf was captured, save it
        if sres.get('cause_list_pdf_b64'):
            pdf_bytes = base64.b64decode(sres['cause_list_pdf_b64'])
            pdf_path = f"cause_list_{date_iso}.pdf"
            with open(pdf_path, 'wb') as pf:
                pf.write(pdf_bytes)
            print(f"[+] Saved cause list PDF to {pdf_path}")
            output['cause_list_pdf'] = pdf_path

    # Save output JSON
    save_json(output, args.out)
    print("[i] Done.")

if __name__ == '__main__':
    main()
