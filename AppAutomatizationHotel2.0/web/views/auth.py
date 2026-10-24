"""Login and logout."""

from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from axes.handlers.proxy import AxesProxyHandler
from axes.helpers import get_credentials, get_failure_limit


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    context = {}
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect("dashboard")
        # The failed authenticate() above was already counted by django-axes
        credentials = get_credentials(username)
        failures = AxesProxyHandler.get_failures(request, credentials)
        # Shown under the login button: one red dot per failed attempt, plus the attempts left
        context = {
            "username": username,
            "remaining_attempts": get_failure_limit(request, credentials) - failures,
            "failed_attempts": range(failures),
        }

    return render(request, "login.html", context)


def logout_view(request):
    logout(request)
    return redirect("login")
