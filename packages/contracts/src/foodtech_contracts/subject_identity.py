"""Opaque Cognito subject within this project's existing key namespace.

Never infer ownership from UUID syntax, email, case folding or normalization.
The authenticated provider must still attest this exact value. Delimiters and
unbounded identifiers are excluded by the project's physical key contract.
"""
import re


def valid_subject(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is not None
