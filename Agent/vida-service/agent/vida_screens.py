"""
VIDA screen state definitions.

Each screen defines:
  - check_regions: pixel areas that uniquely identify this screen
  - elements: known UI element coordinates (in 1024x768 model space)
  - transitions: what actions lead to what screens

Coordinates are in 1024x768 model space (matching piglet's screenshot scaling).
Reference hashes are populated by calibration (POST /api/vida/calibrate).
"""

from enum import Enum
from dataclasses import dataclass, field


class VidaScreen(str, Enum):
    HOME = "home"
    MENU_OPEN = "menu_open"
    PARTS_CATALOG = "parts_catalog"
    SEARCH_FIELD = "search_field"
    SEARCH_RESULTS = "search_results"
    PART_DETAIL = "part_detail"
    VEHICLE_SELECT = "vehicle_select"
    UNKNOWN = "unknown"


@dataclass
class ScreenRegion:
    """A rectangular region used for screen identification."""
    x1: int
    y1: int
    x2: int
    y2: int
    name: str  # e.g., "title_bar", "nav_panel"


@dataclass
class UIElement:
    """A known clickable/typeable UI element."""
    name: str
    x: int  # center x in model coords
    y: int  # center y in model coords
    description: str = ""


@dataclass
class ScreenDefinition:
    """Definition of a known VIDA screen state."""
    screen: VidaScreen
    check_regions: list[ScreenRegion] = field(default_factory=list)
    reference_hashes: dict[str, str] = field(default_factory=dict)  # region_name -> phash hex
    elements: dict[str, UIElement] = field(default_factory=dict)  # element_name -> UIElement


# Default screen definitions with placeholder regions.
# Actual coordinates and hashes are populated during calibration
# by capturing screenshots of each VIDA screen.

SCREEN_DEFINITIONS: dict[VidaScreen, ScreenDefinition] = {
    VidaScreen.HOME: ScreenDefinition(
        screen=VidaScreen.HOME,
        check_regions=[
            ScreenRegion(0, 0, 400, 40, "title_bar"),
            ScreenRegion(0, 40, 60, 300, "nav_sidebar"),
        ],
        elements={
            "hamburger_menu": UIElement("hamburger_menu", 30, 20, "Top-left hamburger menu icon"),
        },
    ),
    VidaScreen.MENU_OPEN: ScreenDefinition(
        screen=VidaScreen.MENU_OPEN,
        check_regions=[
            ScreenRegion(0, 0, 250, 400, "menu_panel"),
        ],
        elements={
            "parts_catalog": UIElement("parts_catalog", 120, 200, "Parts catalog menu item"),
            "vehicle_info": UIElement("vehicle_info", 120, 150, "Vehicle info menu item"),
        },
    ),
    VidaScreen.PARTS_CATALOG: ScreenDefinition(
        screen=VidaScreen.PARTS_CATALOG,
        check_regions=[
            ScreenRegion(0, 0, 400, 40, "title_bar"),
            ScreenRegion(200, 40, 800, 100, "search_area"),
        ],
        elements={
            "search_field": UIElement("search_field", 500, 70, "Part number search input field"),
            "search_button": UIElement("search_button", 700, 70, "Search/magnifying glass button"),
        },
    ),
    VidaScreen.SEARCH_RESULTS: ScreenDefinition(
        screen=VidaScreen.SEARCH_RESULTS,
        check_regions=[
            ScreenRegion(0, 0, 400, 40, "title_bar"),
            ScreenRegion(100, 100, 900, 160, "results_header"),
        ],
        elements={
            "results_table": UIElement("results_table", 500, 400, "Search results table area"),
            "back_button": UIElement("back_button", 50, 70, "Back/return button"),
        },
    ),
    VidaScreen.PART_DETAIL: ScreenDefinition(
        screen=VidaScreen.PART_DETAIL,
        check_regions=[
            ScreenRegion(0, 0, 400, 40, "title_bar"),
            ScreenRegion(100, 50, 600, 120, "part_header"),
        ],
        elements={
            "back_button": UIElement("back_button", 50, 70, "Back to results"),
        },
    ),
}
