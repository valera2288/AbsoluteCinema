from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.hashers import check_password
from django.db.models.functions import Lower

from accounts.models import User
from accounts.throttle import limited_login


class AccountBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        identifier = username if username is not None else kwargs.get(User.USERNAME_FIELD)
        if not identifier or password is None:
            return None
        if request is not None:
            return limited_login(request, identifier, lambda: self.check_credentials(identifier, password))
        return self.check_credentials(identifier, password)

    def check_credentials(self, identifier, password):
        field = "email" if "@" in identifier else "login"
        try:
            user = User.objects.alias(login_key=Lower(field)).get(login_key=identifier.strip().lower())
        except User.DoesNotExist:
            User().set_password(password)
            return None
        if check_password(password, user.password) and self.user_can_authenticate(user):
            return user
        return None
