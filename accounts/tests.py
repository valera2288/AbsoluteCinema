from django.contrib.auth import SESSION_KEY
from django.contrib.auth.models import Group, Permission
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import User

TEST_PASSWORD = "Cinema9-local-test"
TEST_EMAIL = "administrator-long-name@example.test"


@override_settings(SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class AccountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("  CinemaAdmin  ", TEST_EMAIL.upper(), TEST_PASSWORD)

    def test_admin_login_by_login_and_email_uses_db_session(self):
        self.assertEqual(self.admin.login, "cinemaadmin")
        self.assertEqual(self.admin.email, TEST_EMAIL)
        self.assertTrue(self.admin.can_post_reviews)
        login_url = reverse("admin:login")
        for identifier in ("CINEMAADMIN", TEST_EMAIL.upper()):
            with self.subTest(identifier=identifier):
                client = Client(enforce_csrf_checks=True)
                page = client.get(login_url)
                self.assertEqual(page.status_code, 200)
                data = {"username": identifier, "password": TEST_PASSWORD}
                self.assertEqual(client.post(login_url, data).status_code, 403)
                data["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
                response = client.post(login_url, data)
                self.assertRedirects(response, reverse("admin:index"))
                stored = Session.objects.get(session_key=client.session.session_key)
                self.assertEqual(stored.get_decoded()[SESSION_KEY], str(self.admin.pk))

    def test_nonstaff_and_inactive_cannot_use_admin_login(self):
        viewer = User.objects.create_user("viewer", "viewer@example.test", TEST_PASSWORD)
        User.objects.filter(pk=self.admin.pk).update(is_active=False)
        for user in (viewer, self.admin):
            with self.subTest(login=user.login):
                response = self.client.post(reverse("admin:login"), {
                    "username": user.login, "password": TEST_PASSWORD,
                })
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(SESSION_KEY, self.client.session)

    def test_admin_can_create_user_with_custom_fields(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("admin:accounts_user_add"), {
            "login": "  NewViewer  ", "email": "Viewer@Example.Test", "full_name": "Тестовое имя",
            "phone": "+12345678901", "password1": TEST_PASSWORD, "password2": TEST_PASSWORD,
        })
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(login="newviewer", email="viewer@example.test")
        self.assertTrue(user.check_password(TEST_PASSWORD))
        self.assertFalse(user.is_staff)

    def test_postgres_rejects_case_only_duplicates_without_validation(self):
        for values, constraint in (
            ({"login": self.admin.login.upper(), "email": "other@example.test"}, "unique_lower_login"),
            ({"login": "other", "email": self.admin.email.upper()}, "unique_lower_email"),
        ):
            with self.subTest(constraint=constraint):
                with self.assertRaises(IntegrityError) as failure, transaction.atomic():
                    User.objects.bulk_create([User(**values)])
                self.assertEqual(failure.exception.__cause__.diag.constraint_name, constraint)

    def test_roles_are_idempotent_and_staff_cannot_edit_account_permissions(self):
        call_command("setup_roles")
        call_command("setup_roles")
        admin_role = Group.objects.get(name="Администраторы")
        moderator_role = Group.objects.get(name="Модераторы")
        self.assertTrue({"view_user", "publish_movie", "view_auditentry", "change_tariff"}.issubset(
            set(admin_role.permissions.values_list("codename", flat=True))
        ))
        self.assertQuerySetEqual(
            moderator_role.permissions.order_by("codename").values_list("codename", flat=True),
            ["decide_review", "remove_review", "restrict_review_authors", "view_review_queue"],
        )
        staff = User.objects.create_user("staff", "staff@example.test", TEST_PASSWORD, is_staff=True)
        staff.groups.add(admin_role)
        staff.user_permissions.add(Permission.objects.get(codename="change_user", content_type__app_label="accounts"))
        self.client.force_login(staff)
        edit_url = reverse("admin:accounts_user_change", args=[staff.pk])
        self.assertEqual(self.client.get(edit_url).status_code, 200)
        self.assertEqual(self.client.post(edit_url, {"is_superuser": "on"}).status_code, 403)
