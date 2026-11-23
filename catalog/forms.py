from django import forms
from django.core.validators import MaxValueValidator

from catalog.models import Genre, Review, maximum_year


class CatalogFilter(forms.Form):
    q = forms.CharField(
        label="Название", max_length=100, required=False,
        widget=forms.TextInput(attrs={"type": "search", "placeholder": "Название фильма"}),
    )
    genre = forms.ModelMultipleChoiceField(
        label="Жанры", queryset=Genre.objects.filter(is_active=True), required=False,
        widget=forms.CheckboxSelectMultiple,
        error_messages={"invalid_choice": "Выберите действующий жанр из списка.", "invalid_pk_value": "Некорректный жанр."},
    )
    year = forms.IntegerField(label="Год", min_value=1888, required=False)
    min_rating = forms.DecimalField(
        label="Минимальный рейтинг", min_value=1, max_value=5, max_digits=2, decimal_places=1,
        required=False,
    )
    sort = forms.ChoiceField(label="Сортировка", required=False, choices=[
        ("", "Сначала новинки"), ("title", "По названию"), ("year", "По году"), ("rating", "По рейтингу"),
    ])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["year"].max_value = maximum_year()
        self.fields["year"].validators.append(MaxValueValidator(maximum_year()))
        self.fields["year"].widget.attrs["max"] = maximum_year()
        self.fields["genre"].widget.attrs["aria-describedby"] = "id_genre_errors"


class RatingForm(forms.Form):
    profile_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)
    rating = forms.TypedChoiceField(label="Оценка", choices=[(score, score) for score in range(1, 6)], coerce=int)


class ReviewForm(RatingForm):
    text = forms.CharField(label="Отзыв", min_length=10, max_length=2000, widget=forms.Textarea(attrs={"rows": 4}))


class ReviewQueueFilter(forms.Form):
    status = forms.ChoiceField(label="Статус", choices=Review.Status.choices)
    date_from = forms.DateField(label="С даты", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(label="По дату", required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def clean(self):
        data = super().clean()
        if data.get("date_from") and data.get("date_to") and data["date_from"] > data["date_to"]:
            self.add_error("date_to", "Дата окончания не может быть раньше начала.")
        return data
