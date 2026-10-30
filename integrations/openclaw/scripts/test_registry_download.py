import unittest
from urllib.error import URLError
from urllib.request import Request

from registry_download import RegistryRedirectHandler, package_tarball_url


class RegistryDownloadTests(unittest.TestCase):
    def test_tarball_url_is_constructed_from_pinned_package_identity(self):
        self.assertEqual(
            package_tarball_url("undici", "6.29.1"),
            "https://registry.npmjs.org/undici/-/undici-6.29.1.tgz",
        )

    def test_redirects_must_stay_on_https_npm_registry(self):
        handler = RegistryRedirectHandler()
        request = Request("https://registry.npmjs.org/undici/-/undici-6.29.1.tgz")
        redirect = handler.redirect_request(
            request, None, 302, "Found", {},
            "https://registry.npmjs.org/undici/-/undici-6.29.1.tgz?mirror=1",
        )
        self.assertEqual(redirect.full_url, "https://registry.npmjs.org/undici/-/undici-6.29.1.tgz?mirror=1")
        for url in (
            "http://registry.npmjs.org/package.tgz",
            "https://evil.example/package.tgz",
            "https://registry.npmjs.org.evil.example/package.tgz",
            "https://registry.npmjs.org@evil.example/package.tgz",
            "https://registry.npmjs.org:444/package.tgz",
        ):
            with self.subTest(url=url), self.assertRaises(URLError):
                handler.redirect_request(request, None, 302, "Found", {}, url)


if __name__ == "__main__":
    unittest.main()
