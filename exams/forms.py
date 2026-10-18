from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm


class RegistrationForm(UserCreationForm):
    email = forms.EmailField(required=True, help_text="Used to identify you to the organizers.")

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email")

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        if commit:
            user.save()
        return user


class ParticipantLoginForm(AuthenticationForm):
    pass


class StartExamForm(forms.Form):
    accept = forms.BooleanField(
        required=True,
        label="I have read the monitoring notice and I agree to take this exam under these conditions.",
        error_messages={"required": "You must accept the monitoring notice to start."},
    )
