from django.urls import path
from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("users", views.UserViewSet, basename="user")
router.register("roles", views.RoleViewSet, basename="role")

urlpatterns = [
    path("auth/token/", views.LoginView.as_view(), name="auth-token"),
    path("auth/token/refresh/", views.RefreshView.as_view(), name="auth-token-refresh"),
    path("auth/logout/", views.LogoutView.as_view(), name="auth-logout"),
    path("auth/me/", views.MeView.as_view(), name="auth-me"),
    path("auth/password/", views.PasswordChangeView.as_view(), name="auth-password"),
    *router.urls,
]
