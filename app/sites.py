"""
Static list of semiconductor and technology company newsroom URLs to monitor.

Update this list to add, remove, or change sites without touching pipeline logic.
"""

SITES: list[dict[str, str]] = [
    {"name": "NXP Semiconductors", "url": "http://nxp.com/company/about-nxp/newsroom:NEWSROOM"},
    {"name": "Trendforce", "url": "https://www.trendforce.com/presscenter/news"},
    {"name": "STMicroelectronics", "url": "https://newsroom.st.com/"},
    {"name": "Yole Group", "url": "https://www.yolegroup.com/technology-press-release/press-releases/"},
    {"name": "Canalys", "url": "https://www.canalys.com/newsroom"},
    {"name": "ABI Research", "url": "https://www.abiresearch.com/press/"},
    {"name": "SEMI", "url": "https://www.semi.org/en/news-media-press/semi-press-releases"},
    {"name": "Intel", "url": "https://newsroom.intel.com/"},
    {"name": "Synopsys", "url": "https://news.synopsys.com/"},
    {"name": "Keysight", "url": "https://www.keysight.com/in/en/about/newsroom.html"},
    {"name": "Toshiba", "url": "http://toshiba.semicon-storage.com/ap-en/company/news.html"},
    {"name": "Counterpoint Research", "url": "https://www.counterpointresearch.com/blog/"},
    {"name": "Cadence", "url": "https://www.cadence.com/en_US/home/company/newsroom.html"},
    {"name": "Diodes", "url": "https://www.diodes.com/about/news/press-releases/"},
    {"name": "Qualcomm", "url": "https://www.qualcomm.com/news/releases"},
    {"name": "Mitsubishi Electric", "url": "https://www.mitsubishielectric.com/news/index.html"},
    {"name": "Renesas US", "url": "https://www.renesas.com/us/en/about/press-center"},
    {"name": "TDK Electronics", "url": "https://www.tdk-electronics.tdk.com/en/530698/products/product-news"},
    {"name": "Renesas India", "url": "https://www.renesas.com/in/en/about/press-center"},
]
