from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from django.db import connection
from .models import User


class RegisterSerializer(serializers.Serializer):
    email           = serializers.EmailField()
    password        = serializers.CharField(write_only=True, min_length=8)
    full_name_ar    = serializers.CharField(required=False, allow_blank=True)
    full_name_en    = serializers.CharField(required=False, allow_blank=True)
    # Explicit English name parts — the form collects a three-part name now,
    # which beats splitting full_name_en (can't tell middle from last).
    first_name      = serializers.CharField(required=False, allow_blank=True)
    middle_name     = serializers.CharField(required=False, allow_blank=True)
    last_name       = serializers.CharField(required=False, allow_blank=True)
    department_id   = serializers.IntegerField(required=False, allow_null=True)
    academic_rank   = serializers.CharField(required=False, allow_blank=True)
    scholar_id      = serializers.CharField(required=False, allow_blank=True)
    orcid_id        = serializers.CharField(required=False, allow_blank=True)
    scopus_id       = serializers.CharField(required=False, allow_blank=True)

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate_email(self, value):
        value = value.lower().strip()
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("Email already registered")
        with connection.cursor() as cur:
            cur.execute(
                'SELECT 1 FROM "RegistrationRequest" '
                'WHERE LOWER("Email") = LOWER(%s) AND "Status" = %s LIMIT 1',
                [value, 'pending'],
            )
            if cur.fetchone():
                raise serializers.ValidationError("Registration already pending review")
        return value


class VerifyEmailSerializer(serializers.Serializer):
    email = serializers.EmailField()
    token = serializers.CharField(min_length=6)


class LoginSerializer(serializers.Serializer):
    email    = serializers.EmailField()
    password = serializers.CharField(write_only=True)


class UserSerializer(serializers.ModelSerializer):
    role        = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    full_name   = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'user_id', 'litrix_id', 'email', 'full_name', 'full_name_ar',
            'user_type', 'tenant_id', 'role_id', 'role', 'permissions',
            'email_verified', 'scholar_id', 'orcid_id', 'scopus_id',
            'photo_url',
        ]

    def get_role(self, obj):
        return obj.get_role()

    def get_permissions(self, obj):
        return list(obj.get_permissions())

    def get_full_name(self, obj):
        return obj.get_full_name()


class PasswordResetSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    email        = serializers.EmailField()
    token        = serializers.CharField(min_length=6)
    new_password = serializers.CharField(write_only=True, min_length=8)

    def validate_new_password(self, value):
        validate_password(value)
        return value
