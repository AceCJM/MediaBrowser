import os
import tempfile

from django.contrib.auth.models import User, Group
from django.test import TestCase, Client
from django.urls import reverse

from .models import Library
from .views import _safe_join, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS


class LibraryModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("alice", password="pass")
        self.other = User.objects.create_user("bob", password="pass")
        self.superuser = User.objects.create_superuser("admin", password="pass")
        self.group = Group.objects.create(name="media_access")
        self.user.groups.add(self.group)

        self.library = Library.objects.create(name="Test Lib", path="/tmp")

    def test_superuser_always_has_access(self):
        self.assertTrue(self.library.user_has_access(self.superuser))

    def test_direct_user_access(self):
        self.assertFalse(self.library.user_has_access(self.user))
        self.library.users.add(self.user)
        self.assertTrue(self.library.user_has_access(self.user))

    def test_group_access(self):
        self.assertFalse(self.library.user_has_access(self.user))
        self.library.groups.add(self.group)
        self.assertTrue(self.library.user_has_access(self.user))

    def test_no_access_for_unassigned_user(self):
        self.assertFalse(self.library.user_has_access(self.other))

    def test_str(self):
        self.assertEqual(str(self.library), "Test Lib")


class SafeJoinTest(TestCase):
    def test_normal_path(self):
        result = _safe_join("/tmp", "sub/file.txt")
        self.assertTrue(result.startswith("/tmp"))

    def test_path_traversal_raises(self):
        from django.http import Http404
        with self.assertRaises(Http404):
            _safe_join("/tmp/safe", "../../etc/passwd")


class LibraryListViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("alice", password="pass")
        self.tmpdir = tempfile.mkdtemp()
        self.library = Library.objects.create(name="My Lib", path=self.tmpdir)

    def test_redirect_when_not_logged_in(self):
        response = self.client.get(reverse("browser:library_list"))
        self.assertRedirects(response, "/accounts/login/?next=/", fetch_redirect_response=False)

    def test_empty_libraries_for_user_without_access(self):
        self.client.login(username="alice", password="pass")
        response = self.client.get(reverse("browser:library_list"))
        self.assertEqual(response.status_code, 200)
        self.assertQuerySetEqual(response.context["libraries"], [])

    def test_library_visible_after_granting_access(self):
        self.library.users.add(self.user)
        self.client.login(username="alice", password="pass")
        response = self.client.get(reverse("browser:library_list"))
        self.assertIn(self.library, response.context["libraries"])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class BrowseViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user("alice", password="pass")
        self.tmpdir = tempfile.mkdtemp()
        self.library = Library.objects.create(name="My Lib", path=self.tmpdir)
        self.library.users.add(self.user)
        self.client.login(username="alice", password="pass")

    def test_browse_root(self):
        response = self.client.get(reverse("browser:browse_root", args=[self.library.id]))
        self.assertEqual(response.status_code, 200)

    def test_browse_subfolder(self):
        subdir = os.path.join(self.tmpdir, "myfolder")
        os.makedirs(subdir)
        response = self.client.get(reverse("browser:browse", args=[self.library.id, "myfolder"]))
        self.assertEqual(response.status_code, 200)

    def test_media_items_listed(self):
        # Create a small JPEG file
        img_path = os.path.join(self.tmpdir, "test.jpg")
        with open(img_path, "wb") as f:
            # Minimal valid JPEG bytes
            f.write(bytes([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10]) + b"\x00" * 10 + bytes([0xFF, 0xD9]))
        response = self.client.get(reverse("browser:browse_root", args=[self.library.id]))
        filenames = [m["filename"] for m in response.context["media_items"]]
        self.assertIn("test.jpg", filenames)

    def test_no_access_returns_404(self):
        other = User.objects.create_user("bob", password="pass")
        other_lib = Library.objects.create(name="Other", path=self.tmpdir)
        response = self.client.get(reverse("browser:browse_root", args=[other_lib.id]))
        self.assertEqual(response.status_code, 404)

    def test_path_traversal_returns_404(self):
        response = self.client.get(
            reverse("browser:browse", args=[self.library.id, "../../etc"])
        )
        self.assertEqual(response.status_code, 404)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class MediaExtensionTest(TestCase):
    def test_image_extensions_set(self):
        self.assertIn(".jpg", IMAGE_EXTENSIONS)
        self.assertIn(".png", IMAGE_EXTENSIONS)

    def test_video_extensions_set(self):
        self.assertIn(".mp4", VIDEO_EXTENSIONS)
        self.assertIn(".mkv", VIDEO_EXTENSIONS)
