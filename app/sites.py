"""
Static list of semiconductor and technology company newsroom URLs to monitor.

Update this list to add, remove, or change sites without touching pipeline logic.
"""

SITES: list[dict[str, str]] = [
    {"name": "NXP Semiconductors", "url": "https://www.nxp.com/company/about-nxp/newsroom:NEWSROOM"},
    {"name": "Intel", "url": "https://newsroom.intel.com/"},
    {"name": "Qualcomm", "url": "https://www.qualcomm.com/news/releases"},
    {"name": "Synopsys", "url": "https://news.synopsys.com/"},
    {"name": "Cadence", "url": "https://www.cadence.com/en_US/home/company/newsroom.html"},
    {"name": "Texas Instruments", "url": "https://news.ti.com/"},
    {"name": "STMicroelectronics", "url": "https://newsroom.st.com/"},
    {"name": "Renesas", "url": "https://www.renesas.com/en/about/newsroom"},
    {"name": "Infineon", "url": "https://www.infineon.com/cms/en/about-infineon/press/press-releases/"},
    {"name": "Microchip Technology", "url": "https://www.microchip.com/en-us/about/media-center/news-releases"},
    {"name": "onsemi", "url": "https://www.onsemi.com/site/news"},
    {"name": "MediaTek", "url": "https://www.mediatek.com/news-events/news"},
    {"name": "NVIDIA", "url": "https://nvidianews.nvidia.com/"},
    {"name": "AMD", "url": "https://www.amd.com/en/newsroom.html"},
    {"name": "Arm", "url": "https://newsroom.arm.com/"},
    {"name": "TSMC", "url": "https://www.tsmc.com/english/news"},
    {"name": "Samsung Semiconductor", "url": "https://semiconductor.samsung.com/us/newsroom/"},
    {"name": "Marvell", "url": "https://www.marvell.com/company/newsroom/"},
    {"name": "Broadcom", "url": "https://investors.broadcom.com/news-releases"},
]
