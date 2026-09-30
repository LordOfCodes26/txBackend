from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.functional import cached_property

from common.models import TimeStampedModel


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("Email is required.")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def get_by_natural_key(self, username):
        return self.get(email__iexact=username)

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra["is_staff"] = True
        extra["is_superuser"] = True
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser):
    """Login identity. Business profiles (Developer, Seller) link to this one-to-one.

    Authorization goes through RBAC (`Role` → `Permission`), not Django's built-in
    groups/permissions. `is_superuser` is a break-glass flag that bypasses RBAC and is
    the only way into the Django admin.
    """

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    roles = models.ManyToManyField(
        "Role", through="UserRole", through_fields=("user", "role"), related_name="users"
    )

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("email"), name="accounts_user_email_ci_unique"),
        ]

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        self.email = self.email.lower()
        super().save(*args, **kwargs)

    # --- RBAC ---------------------------------------------------------------

    @cached_property
    def rbac_permissions(self) -> frozenset[str]:
        if not self.is_active:
            return frozenset()
        qs = Permission.objects.all()
        if not self.is_superuser:
            qs = qs.filter(roles__user_roles__user=self).distinct()
        return frozenset(qs.values_list("codename", flat=True))

    def has_rbac_perm(self, codename: str) -> bool:
        return codename in self.rbac_permissions

    def has_rbac_perms(self, codenames) -> bool:
        return set(codenames) <= self.rbac_permissions

    def invalidate_rbac_cache(self) -> None:
        self.__dict__.pop("rbac_permissions", None)

    # --- Django admin hooks (superuser only) -------------------------------

    def has_perm(self, perm, obj=None) -> bool:
        return self.is_active and self.is_superuser

    def has_module_perms(self, app_label) -> bool:
        return self.is_active and self.is_superuser


class Permission(models.Model):
    """A granular capability such as `developer.view`. Defined in code (`rbac.PERMISSIONS`)
    and synced to the DB so roles can be edited without a deploy."""

    codename = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["codename"]

    def __str__(self):
        return self.codename


class Role(TimeStampedModel):
    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True)
    is_system = models.BooleanField(default=False)
    permissions = models.ManyToManyField(Permission, related_name="roles", blank=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code


class UserRole(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="user_roles")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="user_roles")
    assigned_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "role"], name="accounts_userrole_unique"),
        ]

    def __str__(self):
        return f"{self.user} → {self.role}"
