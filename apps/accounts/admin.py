from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Permission, Role, User, UserRole
from .rbac import Roles


class UserRoleInline(admin.TabularInline):
    model = UserRole
    fk_name = "user"
    extra = 0
    readonly_fields = ["assigned_by", "assigned_at"]


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ["email"]
    list_display = ["email", "full_name", "is_active", "is_superuser", "last_login"]
    list_filter = ["is_active", "is_superuser", "roles"]
    search_fields = ["email", "full_name"]
    filter_horizontal = []
    inlines = [UserRoleInline]
    fieldsets = [
        (None, {"fields": ["email", "password"]}),
        ("Profile", {"fields": ["full_name"]}),
        ("Status", {"fields": ["is_active", "is_staff", "is_superuser"]}),
        ("Dates", {"fields": ["last_login", "date_joined"]}),
    ]
    add_fieldsets = [
        (None, {"classes": ["wide"], "fields": ["email", "password1", "password2"]}),
    ]


# Roles whose users reach only their *own* data through a link (Seller.user,
# ServicePosition.manager, Developer.user). A global permission on them means "everything":
# e.g. good.view lets every seller see every store's goods.
SELF_SERVICE_ROLES = {Roles.SELLER, Roles.DEVELOPER}


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "is_system"]
    filter_horizontal = ["permissions"]

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        role = form.instance
        granted = sorted(role.permissions.values_list("codename", flat=True))
        if role.code in SELF_SERVICE_ROLES and granted:
            messages.warning(
                request,
                f"{role.code} users get these permissions for ALL data, not only their own: "
                f"{', '.join(granted)}. Sellers then see every store's goods and sales. "
                f"Leave this role without permissions unless that is intended.",
            )


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ["codename", "description"]
    search_fields = ["codename"]
