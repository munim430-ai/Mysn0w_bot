#!/usr/bin/env python3
"""
RSC HTML Structure Diagnostic
Run this first to see how the factory cards are structured
"""

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()

url = "https://rsc-bd.org/factories/"
resp = requests.get(url, timeout=30, verify=False, headers={
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
})

soup = BeautifulSoup(resp.text, 'html.parser')

# Find all divs that contain "Remediation Status" 
factory_divs = [d for d in soup.find_all('div') 
                if d.find(string=lambda t: t and 'Remediation Status' in t)]

print(f"Found {len(factory_divs)} potential factory cards\n")

if factory_divs:
    first = factory_divs[0]
    print("=== FIRST FACTORY CARD HTML ===")
    print(first.prettify()[:2000])
    print("\n=== FIRST FACTORY CARD TEXT ===")
    print(first.get_text(separator='\n', strip=True))
    
    print("\n=== ALL LINKS IN FIRST CARD ===")
    for a in first.find_all('a', href=True):
        print(f"  Text: '{a.get_text(strip=True)}' | href: {a['href'][:80]}")
    
    print("\n=== ALL IMAGES IN FIRST CARD ===")
    for img in first.find_all('img'):
        print(f"  src: {img.get('src', 'N/A')[:60]} | alt: {img.get('alt', 'N/A')}")
