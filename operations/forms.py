from django import forms
from django.utils import timezone

from operations.models import SupportMessage


class SupportForm(forms.Form):
    subject = forms.CharField(label="Тема", min_length=5, max_length=100)
    body = forms.CharField(label="Сообщение", min_length=10, max_length=2000, widget=forms.Textarea(attrs={"rows": 6}))


class ReplyForm(forms.Form):
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    reply_code = forms.ChoiceField(label="Готовый ответ", required=False, choices=[("", "Без ответа"), *SupportMessage.Reply.choices])
    body = forms.CharField(label="Дополнение", required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 4}))
    action = forms.ChoiceField(label="Статус после сохранения", choices=[("work", "В работе"), ("close", "Закрыто")])


class MarketingForm(forms.Form):
    marketing_opt_in = forms.BooleanField(label="Получать новости кинотеатра", required=False)


class ReportForm(forms.Form):
    kind = forms.ChoiceField(label="Отчёт", choices=[("billing", "Подписки и возвраты"), ("views", "Просмотры")])
    start = forms.DateField(label="С даты", widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))
    end = forms.DateField(label="По дату", widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end"):
            if data["start"] > data["end"]:
                self.add_error("end", "Конец периода раньше начала.")
            elif data["start"].year < 1900 or data["end"].year > 9998:
                self.add_error("end", "Укажите даты между 1900 и 9998 годами.")
        return data

    @classmethod
    def default(cls):
        today = timezone.localdate()
        return {"kind": "billing", "start": today.replace(day=1), "end": today}
