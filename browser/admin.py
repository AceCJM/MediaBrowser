from django.contrib import admin
from .models import Library


@admin.register(Library)
class LibraryAdmin(admin.ModelAdmin):
    list_display = ("name", "path", "description")
    search_fields = ("name", "path")
    filter_horizontal = ("users", "groups")
