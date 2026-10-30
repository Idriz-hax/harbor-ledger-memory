from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener


class RegistryRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        parsed = urlsplit(new_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "registry.npmjs.org"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise URLError("Blocked redirect outside npm registry")
        return super().redirect_request(request, fp, code, message, headers, new_url)


def package_tarball_url(name, version):
    return f"https://registry.npmjs.org/{name}/-/{name}-{version}.tgz"


REGISTRY_OPENER = build_opener(RegistryRedirectHandler())
