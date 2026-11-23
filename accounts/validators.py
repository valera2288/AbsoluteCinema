from django.core.exceptions import ValidationError


class AccountPasswordValidator:
    def validate(self, password, user=None):
        if (
            not 8 <= len(password) <= 64
            or not any(char.isalpha() for char in password)
            or not any(char.isdecimal() for char in password)
        ):
            raise ValidationError(self.get_help_text(), code="invalid_account_password")

    def get_help_text(self):
        return "Пароль должен содержать от 8 до 64 символов, хотя бы одну букву и одну цифру."
