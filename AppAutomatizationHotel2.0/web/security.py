"""Security helpers wired from settings (brute force protection)."""


def get_client_ip(request):
    """
    Client IP for django-axes. PythonAnywhere's proxy sets X-Real-IP to the real visitor address;
    without it every attempt would share the proxy IP and one lockout would block everybody.
    """
    return request.META.get("HTTP_X_REAL_IP") or request.META.get("REMOTE_ADDR")
