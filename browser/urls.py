from django.urls import path
from . import views
from django.views.generic import TemplateView

app_name = "browser"

urlpatterns = [
    path("", views.library_list, name="library_list"),
    path("library/<int:library_id>/", views.browse, name="browse_root"),
    path("library/<int:library_id>/browse/<path:subpath>", views.browse, name="browse"),
    path("library/<int:library_id>/play/<path:filepath>", views.media_player, name="media_player"),
    path("library/<int:library_id>/media/<path:filepath>", views.serve_media, name="serve_media"),
    path("library/<int:library_id>/thumbnail/<path:filepath>", views.serve_thumbnail, name="serve_thumbnail"),
    path("library/<int:library_id>/download/", views.download_videos, name="download_videos"),
    path("library/<int:library_id>/download/progress/", views.download_progress, name="download_progress"),
    path("active-downloads/", views.active_downloads, name="active_downloads"),
    path("cancel-download/", views.cancel_download, name="cancel_download"),
    # PWA support
    path("manifest.json", TemplateView.as_view(template_name="browser/manifest.json", content_type="application/manifest+json"), name="manifest"),
    path("sw.js", TemplateView.as_view(template_name="browser/sw.js", content_type="application/javascript"), name="service_worker"),
]
