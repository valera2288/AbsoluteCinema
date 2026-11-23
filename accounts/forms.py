from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AuthenticationForm, BaseUserCreationForm, UserChangeForm
from django.db.models.functions import Lower

from accounts.models import Profile, User


class UniqueIdentityForm:
    def clean(self):
        cleaned = super().clean()
        for field, label in (("login", "логин"), ("email", "email")):
            value = cleaned.get(field)
            if value:
                normalized = value.strip().lower()
                if User.objects.exclude(pk=self.instance.pk).alias(identity=Lower(field)).filter(identity=normalized).exists():
                    self.add_error(field, f"Этот {label} уже занят.")
                else:
                    cleaned[field] = normalized
        return cleaned


class AccountCreationForm(UniqueIdentityForm, BaseUserCreationForm):
    class Meta:
        model = User
        fields = ("login", "email", "full_name", "phone")


class AccountChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User


class AdminLoginForm(AdminAuthenticationForm):
    username = forms.CharField(label="Логин или email", max_length=254)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].max_length = 254
        self.fields["username"].widget.attrs["maxlength"] = 254


class ViewerLoginForm(AuthenticationForm):
    username = forms.CharField(label="Логин или email", max_length=254)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].max_length = 254
        self.fields["username"].widget.attrs["maxlength"] = 254


class ViewerDetailsForm(UniqueIdentityForm, forms.ModelForm):
    class Meta:
        model = User
        fields = ("login", "email", "full_name", "phone")


class PasswordConfirmation(forms.Form):
    current_password = forms.CharField(label="Текущий пароль", max_length=128,
                                      widget=forms.PasswordInput, strip=False, required=True)


class ProfileForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ("profile_name", "is_kids")


class ProfileNameForm(ProfileForm):
    class Meta(ProfileForm.Meta):
        fields = ("profile_name",)


class GuardForm(forms.Form):
    pin = forms.RegexField(r"\A[0-9]{4}\Z", label="PIN исходного детского профиля", required=False,
                           widget=forms.PasswordInput(attrs={"inputmode": "numeric", "maxlength": "4"}))
    current_password = forms.CharField(label="Пароль аккаунта, если исходный PIN больше недоступен",
                                      max_length=128, required=False, widget=forms.PasswordInput, strip=False)


class PinForm(PasswordConfirmation):
    pin = forms.RegexField(r"\A[0-9]{4}\Z", label="Новый PIN (пустое поле отключает PIN)", required=False,
                           widget=forms.PasswordInput(attrs={"inputmode": "numeric", "maxlength": "4"}))
