from django.urls import path
from . import views

app_name = "browser"

urlpatterns = [
    path("", views.library_list, name="library_list"),
    path("library/<int:library_id>/", views.browse, name="browse_root"),
    path("library/<int:library_id>/browse/<path:subpath>", views.browse, name="browse"),
    path("library/<int:library_id>/play/<path:filepath>", views.media_player, name="media_player"),
    path("library/<int:library_id>/media/<path:filepath>", views.serve_media, name="serve_media"),
    path("library/<int:library_id>/thumbnail/<path:filepath>", views.serve_thumbnail, name="serve_thumbnail"),
]
