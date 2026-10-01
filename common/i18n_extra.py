"""Framework messages (DRF, simplejwt, Django password validators) that API users see.

Listed here so `makemessages` puts them in our catalog: the Korean (DPRK) translations in
locale/ko_KP override the frameworks' own South Korean or missing translations. The strings
must match the frameworks' source exactly (tests/test_i18n.py checks this).
"""

from django.utils.translation import gettext_noop, ngettext_lazy

DRF = [
    gettext_noop("This field is required."),
    gettext_noop("This field may not be null."),
    gettext_noop("This field may not be blank."),
    gettext_noop("Invalid input."),
    gettext_noop("Not found."),
    gettext_noop("Malformed request."),
    gettext_noop("Incorrect authentication credentials."),
    gettext_noop("Authentication credentials were not provided."),
    gettext_noop("You do not have permission to perform this action."),
    gettext_noop('Method "{method}" not allowed.'),
    gettext_noop('Unsupported media type "{media_type}" in request.'),
    gettext_noop("Request was throttled."),
    gettext_noop("A server error occurred."),
    gettext_noop("A valid integer is required."),
    gettext_noop("A valid number is required."),
    gettext_noop("Must be a valid boolean."),
    gettext_noop("Not a valid string."),
    gettext_noop("Enter a valid email address."),
    gettext_noop("Ensure this field has no more than {max_length} characters."),
    gettext_noop("Ensure this field has at least {min_length} characters."),
    gettext_noop("Ensure this value is less than or equal to {max_value}."),
    gettext_noop("Ensure this value is greater than or equal to {min_value}."),
    gettext_noop("Ensure that there are no more than {max_digits} digits in total."),
    gettext_noop("Ensure that there are no more than {max_decimal_places} decimal places."),
    gettext_noop(
        "Ensure that there are no more than {max_whole_digits} digits before the decimal point."
    ),
    gettext_noop("Date has wrong format. Use one of these formats instead: {format}."),
    gettext_noop("Datetime has wrong format. Use one of these formats instead: {format}."),
    gettext_noop("Time has wrong format. Use one of these formats instead: {format}."),
    gettext_noop('"{input}" is not a valid choice.'),
    gettext_noop('Expected a list of items but got type "{input_type}".'),
    gettext_noop("This list may not be empty."),
    gettext_noop('Invalid pk "{pk_value}" - object does not exist.'),
    gettext_noop("Incorrect type. Expected pk value, received {data_type}."),
    gettext_noop("This field must be unique."),
    gettext_noop("No file was submitted."),
    gettext_noop("The submitted file is empty."),
    gettext_noop(
        "Upload a valid image. The file you uploaded was either not an image or a corrupted image."
    ),
    gettext_noop("Invalid page."),
]

SIMPLEJWT = [
    gettext_noop("No active account found with the given credentials"),
    gettext_noop("Token is invalid or expired"),
    gettext_noop("Token is invalid"),
    gettext_noop("Token is expired"),
    gettext_noop("Token is blacklisted"),
    gettext_noop("Given token not valid for any token type"),
    gettext_noop("User not found"),
    gettext_noop("User is inactive"),
    gettext_noop("Authorization header must contain two space-delimited values"),
]

PASSWORD_VALIDATORS = [
    ngettext_lazy(
        "This password is too short. It must contain at least %(min_length)d character.",
        "This password is too short. It must contain at least %(min_length)d characters.",
        "min_length",
    ),
    gettext_noop("This password is too common."),
    gettext_noop("This password is entirely numeric."),
    gettext_noop("The password is too similar to the %(verbose_name)s."),
]
