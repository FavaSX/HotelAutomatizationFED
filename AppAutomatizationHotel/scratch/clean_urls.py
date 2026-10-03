import re

def clean_urls():
    with open('web/urls.py', 'r', encoding='utf-8') as f:
        content = f.read()

    # Remove the QR routes
    content = re.sub(r"\s*path\('envio_qr'.*?\n", "\n", content)
    content = re.sub(r"\s*path\('qr'.*?\n", "\n", content)

    with open('web/urls.py', 'w', encoding='utf-8') as f:
        f.write(content)

clean_urls()
print("URLs cleaned")
