from django.db import models
from django.contrib.auth.models import User, Group


class Library(models.Model):
    name = models.CharField(max_length=200)
    path = models.CharField(max_length=1000, help_text="Absolute filesystem path to the media directory")
    description = models.TextField(blank=True)
    users = models.ManyToManyField(
        User,
        blank=True,
        related_name="libraries",
        help_text="Users with access to this library",
    )
    groups = models.ManyToManyField(
        Group,
        blank=True,
        related_name="libraries",
        help_text="Groups with access to this library",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "libraries"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def user_has_access(self, user):
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        if self.users.filter(pk=user.pk).exists():
            return True
        user_group_ids = user.groups.values_list("pk", flat=True)
        if self.groups.filter(pk__in=user_group_ids).exists():
            return True
        return False
