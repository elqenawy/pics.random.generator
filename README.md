# Random Photo Finder

A small local web app that picks a random photo from a public website.

## What changed in this version

The scanner checks more than basic `<img src="...">` tags. It now looks for:

- Regular image tags, `srcset`, `<picture><source>`, and common lazy-load attributes
- OpenGraph/Twitter image metadata and structured JSON-LD image fields
- CSS `background-image` URLs and video poster images
- Links directly to common image file types
- Up to 10 pages on the same site, prioritizing links that look like galleries, photos, products, albums, or portfolios
- Up to 750 unique image candidates per scan

It also avoids repeating the current image when you click **Another photo**, until the candidate list has been used up. A successful scan is cached in memory for 15 minutes, so the first search may still take a little while, but subsequent photo picks reuse the collected results instead of scanning the website again. The cache holds up to 20 website scans and clears as the app process restarts.

## Run locally

Requires Python 3.10 or newer.

1. Extract this folder.
2. Open a terminal in the `random-photo-finder` folder.
3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Start the app:

   ```bash
   python app.py
   ```

5. Open <http://127.0.0.1:5000> in your browser.

## Limitations

This is a lightweight HTML scanner, not a full browser crawler. It cannot see images that appear only after JavaScript executes, content hidden behind logins, private pages, or websites that block automated requests. It checks a limited number of same-site pages and stops after a time budget; therefore it cannot guarantee every image on a large website. For the best coverage, enter a gallery, category, portfolio, or product listing URL.

Only public HTTP/HTTPS URLs are supported. Local and private-network host addresses are rejected. The app is intended for local use; it does not require an account or API key.
