"""
Static list of semiconductor and technology company newsroom URLs to monitor.

Update this list to add, remove, or change sites without touching pipeline logic.

Keys:
    name: Source name shown in the digest. Must be unique.
    url:  The newsroom / press-release listing page.
    feed: Optional RSS/Atom feed carrying the same items as the listing page. When set,
          the feed is used first: it is free, fast, and gives exact titles, links and
          dates. The listing page is used automatically if the feed fails.
"""

SITES: list[dict[str, str]] = [
    {"name": "NXP Semiconductors", "url": "http://nxp.com/company/about-nxp/newsroom:NEWSROOM"},
    {"name": "Trendforce", "url": "https://www.trendforce.com/presscenter/news"},
    {"name": "STMicroelectronics", "url": "https://newsroom.st.com/"},
    {"name": "Yole Group", "url": "https://www.yolegroup.com/technology-press-release/press-releases/"},
    # Canalys is now part of Omdia; canalys.com/newsroom stopped publishing in Sept 2025.
    {"name": "Omdia (incl. Canalys)", "url": "https://omdia.tech.informa.com/pr"},
    {"name": "ABI Research", "url": "https://www.abiresearch.com/press/"},
    {"name": "SEMI", "url": "https://www.semi.org/en/news-media-press/semi-press-releases"},
    {"name": "Intel", "url": "https://newsroom.intel.com/"},
    {
        "name": "Synopsys",
        "url": "https://news.synopsys.com/",
        "feed": "https://news.synopsys.com/home?pagetemplate=rss",
    },
    {"name": "Keysight", "url": "https://www.keysight.com/in/en/about/newsroom.html"},
    {"name": "Toshiba", "url": "http://toshiba.semicon-storage.com/ap-en/company/news.html"},
    {"name": "Counterpoint Research", "url": "https://counterpointresearch.com/en/insights?category=research-briefs-blogs"},
    {
        "name": "Cadence",
        "url": "https://www.cadence.com/en_US/home/company/newsroom.html",
        "feed": "https://newsroom.cadence.com/rss/pressrelease.aspx",
    },
    {
        "name": "Diodes",
        "url": "https://www.diodes.com/about/news/press-releases/",
        "feed": "https://www.diodes.com/about/news/press-releases/rss",
    },
    {"name": "Qualcomm", "url": "https://www.qualcomm.com/news/releases"},
    {"name": "Mitsubishi Electric", "url": "https://www.mitsubishielectric.com/news/index.html"},
    # The former "Renesas US" and "Renesas India" URLs both redirect to the same global newsroom.
    {"name": "Renesas", "url": "https://www.renesas.com/us/en/about/press-center"},
    {"name": "TDK Electronics", "url": "https://www.tdk-electronics.tdk.com/en/530698/products/product-news"},
    {"name": "SIA", "url": "https://www.semiconductors.org/news-events/latest-news/"},
    {"name": "Qorvo", "url": "https://www.qorvo.com/newsroom/news"},
    {"name": "IPC (Global Electronics Association)", "url": "https://www.ipc.org/news"},
    {"name": "Panasonic Industrial (NA)", "url": "https://na.industrial.panasonic.com/whats-new"},
    {"name": "GlobalFoundries", "url": "https://gf.com/news-events/globalfoundries-press-releases/"},
    {"name": "MaxLinear", "url": "https://www.maxlinear.com/company/press-releases"},
    # ckswitches.com/news redirects to this same Littelfuse page (C&K is part of Littelfuse).
    {"name": "Littelfuse (incl. C&K Switches)", "url": "https://www.littelfuse.com/about-us/news/news-releases.aspx"},
    {"name": "Murata", "url": "https://www.murata.com/en-us/news"},
    {"name": "Vishay", "url": "https://www.vishay.com/company/press/"},
    {"name": "Tower Semiconductor", "url": "https://towersemi.com/news-events/press-release-page/"},
    {
        "name": "AMD",
        "url": "https://www.amd.com/en/newsroom.html",
        "feed": "https://newsroom.amd.com/rss.xml",
    },
    {
        "name": "Apple",
        "url": "https://www.apple.com/newsroom/",
        "feed": "https://www.apple.com/newsroom/rss-feed.rss",
    },
    {"name": "Texas Instruments", "url": "https://news.ti.com/"},
    {"name": "Valeo", "url": "https://www.valeo.com/en/press-releases/"},
    {"name": "onsemi", "url": "https://www.onsemi.com/company/news-media/press-announcements"},
    {"name": "TT Electronics", "url": "https://www.ttelectronics.com/news-events/news/"},
    {"name": "Bourns", "url": "https://www.bourns.com/news/press-releases"},
    # synaptics.com/company/newsroom redirects to /company/news.
    {"name": "Synaptics", "url": "https://www.synaptics.com/company/news"},
    # The old fujitsu.com press-release URL now returns 404; this is its replacement.
    {"name": "Fujitsu", "url": "https://global.fujitsu/en-global/pr"},
    {"name": "u-blox", "url": "https://www.u-blox.com/en/newsroom"},
    {"name": "u-blox Investor Relations", "url": "https://www.u-blox.com/en/investor-relations"},
    {"name": "Smiths Interconnect", "url": "https://www.smithsinterconnect.com/news-events/latest-news/"},
    {"name": "SK hynix", "url": "https://news.skhynix.com/"},
    {"name": "Gartner", "url": "https://www.gartner.com/en/newsroom/archive"},
    {"name": "Socionext", "url": "https://www.socionext.com/en/topics/index.html"},
    {"name": "TSMC", "url": "https://pr.tsmc.com/english/latest-news"},
    {"name": "pSemi", "url": "https://www.psemi.com/newsroom"},
    {"name": "Analog Devices", "url": "https://www.analog.com/en/about-adi/news-room/press-releases.html"},
    {"name": "Panasonic", "url": "https://news.panasonic.com/global/press/"},
    {
        "name": "Arm",
        "url": "https://www.arm.com/company/news",
        "feed": "https://newsroom.arm.com/news/feed/",
    },
    {
        "name": "QuickLogic",
        "url": "https://ir.quicklogic.com/press-releases",
        "feed": "https://ir.quicklogic.com/rss",
    },
    {"name": "Marvell", "url": "https://www.marvell.com/company/newsroom.html"},
    {"name": "Emerson", "url": "https://www.emerson.com/en-us/news"},
    {"name": "BorgWarner", "url": "https://www.borgwarner.com/newsroom"},
    {"name": "Lantronix", "url": "https://www.lantronix.com/newsroom/press-releases/"},
    {
        "name": "Entegris",
        "url": "https://investor.entegris.com/news-releases",
        "feed": "https://investor.entegris.com/rss/pressrelease.aspx",
    },
    {"name": "Broadcom", "url": "https://www.broadcom.com/company/news"},
    {"name": "Arteris", "url": "https://www.arteris.com/press-releases"},
    {"name": "VeriSilicon", "url": "https://www.verisilicon.com/en/PressRelease"},
    {
        "name": "JEDEC",
        "url": "https://www.jedec.org/news/pressreleases",
        "feed": "https://www.jedec.org/news/rss.xml/pressreleases",
    },
    {"name": "Wind River", "url": "https://www.windriver.com/news/press/"},
    {
        "name": "Digi International",
        "url": "https://www.digi.com/news/press-releases",
        "feed": "https://www.digi.com/news/press-releases?rss=Digi-International-Press-Releases",
    },
    {"name": "ROHM", "url": "https://www.rohm.com/news-detail?defaultGroupId=false"},
    {
        "name": "MediaTek",
        "url": "https://corp.mediatek.com/news-events/press-releases",
        "feed": "https://www.mediatek.com/press-room/rss.xml",
    },
    {"name": "Tachyum", "url": "https://www.tachyum.com/media/press-releases/"},
    {"name": "Andes Technology", "url": "https://www.andestech.com/en/press-release/"},
    {"name": "Achronix", "url": "https://www.achronix.com/company/newsroom/press-releases"},
    {"name": "Imagination Technologies", "url": "https://www.imaginationtech.com/news/"},
    {
        "name": "MACOM",
        "url": "https://ir.macom.com/news-releases",
        "feed": "https://ir.macom.com/rss/news-releases.xml",
    },
    # news.strategyanalytics.com no longer resolves (Strategy Analytics was absorbed into
    # TechInsights), so it is not tracked.
    {"name": "Navitas Semiconductor", "url": "https://navitassemi.com/press-releases/"},
    # The Siemens PLM newsroom URL redirects to the global Siemens newsroom.
    {"name": "Siemens", "url": "https://www.plm.automation.siemens.com/global/en/our-story/newsroom/"},
    {
        "name": "IBM",
        "url": "https://newsroom.ibm.com/",
        "feed": "https://newsroom.ibm.com/announcements?pagetemplate=rss",
    },
    {"name": "IBM Featured Stories", "url": "https://newsroom.ibm.com/featured-stories"},
    {"name": "Lattice Semiconductor", "url": "https://www.latticesemi.com/About/Newsroom"},
    {"name": "Eaton", "url": "https://www.eaton.com/us/en-us/company/news-insights/news-releases.html"},
    {"name": "Creonic", "url": "https://www.creonic.com/news"},
    {"name": "Kneron", "url": "https://www.kneron.com/news/blog/"},
    {"name": "Insyde Software", "url": "https://www.insyde.com/press_news/press-releases-archive"},
]


def select_sites(pattern: str) -> list[dict[str, str]]:
    """Sites whose name or URL contains any of the comma-separated words ('all' = every site)."""
    if pattern.strip().lower() == "all":
        return list(SITES)
    terms = [t.strip().lower() for t in pattern.split(",") if t.strip()]
    return [s for s in SITES if any(t in s["name"].lower() or t in s["url"].lower() for t in terms)]
