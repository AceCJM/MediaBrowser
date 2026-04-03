# MediaBrowser

A Django-based media browser that lets you host and stream videos and photos from configured directories.

## Features

- **Libraries** — Administrators configure directories on disk as named libraries via the Django admin.
- **User & group permissions** — Each library can be restricted to specific users and/or groups. Superusers always see all libraries.
- **Subfolder browsing** — Navigate through subdirectories within a library with breadcrumb navigation.
- **Thumbnail grid** — Media files are shown as thumbnail cards with titles and type badges (Video / Image).
- **HTML5 media player** — Click any file to open a full-screen-friendly player with a prominent *Back to folder* button.
- **Image thumbnails** — Generated on-demand using Pillow and cached on disk.
- **Video placeholders** — SVG play-button overlay served when no video thumbnail is available.

## Requirements

- Python ≥ 3.10
- `pip install -r requirements.txt`

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Apply database migrations
python manage.py migrate

# 3. Create a superuser
python manage.py createsuperuser

# 4. Start the development server
python manage.py runserver
```

Then open `http://127.0.0.1:8000/` in a browser, sign in, and go to the **Admin** panel (`/admin/`) to create a **Library** pointing at a directory that contains your media files.

## Project layout

```
mediabrowser/   Django project settings & URL config
browser/        Main application
  models.py     Library model (name, path, users, groups)
  views.py      Library list, folder browser, media player, file serving & thumbnails
  urls.py       URL patterns
  admin.py      Library admin registration
  templates/
    browser/    HTML templates (base, library_list, browse, media_player)
    registration/login.html
  tests.py      Unit & integration tests
```

## Running the test suite

```bash
python manage.py test browser
```
