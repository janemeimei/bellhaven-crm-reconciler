import unittest

from bellhaven.models import BASE_URL
from bellhaven.scrape import public_url, scrape_website


def detail(name, street):
    return (
        f"<html><body><div class='wrap'><h1>{name}</h1><dl class='detail'>"
        f"<dt>Address</dt><dd>{street}<br>Exampleton, OH 43000</dd>"
        "<dt>Care Offerings</dt><dd><span class='badge'>Assisted Living</span></dd>"
        "</dl></div></body></html>"
    )


class ScrapeTests(unittest.TestCase):
    def pages(self):
        return {
            BASE_URL + "/": "<body><a href='/communities/new'>New community</a></body>",
            BASE_URL + "/about": "<body>Example operator context.</body>",
            BASE_URL + "/communities": (
                "<body><div class='wrap'>Page 1 of 2 - 2 communities listed"
                "<div class='card'><h3><a href='/communities/a'>A</a></h3></div>"
                "<a href='/communities?page=2'>Next</a></div></body>"
            ),
            BASE_URL + "/communities?page=2": (
                "<body><div class='wrap'>Page 2 of 2 - 2 communities listed"
                "<div class='card'><h3><a href='/communities/b'>B</a></h3></div>"
                "<a href='/communities?page=1'>Previous</a></div></body>"
            ),
            BASE_URL + "/communities/a": detail("Community A", "1 Example St"),
            BASE_URL + "/communities/b": detail("Community B", "2 Example St"),
            BASE_URL + "/communities/new": detail("New Community", "3 Example St"),
        }

    def test_all_pages_and_homepage_only_community_are_collected(self):
        result = scrape_website(self.pages().__getitem__)
        self.assertTrue(result.complete, result.errors)
        self.assertEqual(result.directory_count, 2)
        self.assertEqual(len(result.locations), 3)
        self.assertTrue(result.warnings)

    def test_missing_pagination_is_not_reported_as_complete(self):
        pages = self.pages()
        pages[BASE_URL + "/communities"] = pages[BASE_URL + "/communities"].replace(
            "<a href='/communities?page=2'>Next</a>", ""
        )
        self.assertFalse(scrape_website(pages.__getitem__).complete)

    def test_bad_detail_blocks_absence_inferences(self):
        pages = self.pages()
        pages[BASE_URL + "/communities/a"] = "<body><h1>Broken detail</h1></body>"
        result = scrape_website(pages.__getitem__)
        self.assertFalse(result.complete)
        self.assertTrue(result.errors)

    def test_crawler_never_follows_private_or_external_links(self):
        self.assertIsNone(public_url("/crm/private-value/accounts"))
        self.assertIsNone(public_url("/api/v1/accounts"))
        self.assertIsNone(public_url("https://example.com/communities/a"))
        self.assertIsNone(public_url("/communities?token=private-value"))
        self.assertEqual(public_url("/communities?page=1"), BASE_URL + "/communities")


if __name__ == "__main__":
    unittest.main()
