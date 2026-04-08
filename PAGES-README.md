# GitHub Pages Setup

This folder now contains a minimal static site for Fitbit app registration:

- `index.html`
- `privacy.html`
- `terms.html`

## Fastest publish path

1. Create a new public GitHub repository, for example `fitbit-coach`.
2. Upload these three HTML files to the root of that repository.
3. In GitHub, open `Settings` -> `Pages`.
4. Under `Build and deployment`, choose:
   - `Source`: `Deploy from a branch`
   - `Branch`: `main`
   - `Folder`: `/ (root)`
5. Save and wait for GitHub Pages to publish.

## Expected URLs

If the repository name is `fitbit-coach`, the URLs will usually be:

- `https://carolinemeeks.github.io/fitbit-coach/`
- `https://carolinemeeks.github.io/fitbit-coach/privacy.html`
- `https://carolinemeeks.github.io/fitbit-coach/terms.html`

## Fitbit registration values

- `Application Website URL`: `https://carolinemeeks.github.io/fitbit-coach/`
- `Organization URL`: `https://github.com/CarolineMeeks`
- `Privacy Policy URL`: `https://carolinemeeks.github.io/fitbit-coach/privacy.html`
- `Terms of Service URL`: `https://carolinemeeks.github.io/fitbit-coach/terms.html`
- `Callback URL`: `http://127.0.0.1:8765/callback`
