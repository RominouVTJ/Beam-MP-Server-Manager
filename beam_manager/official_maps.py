from beam_manager.models import OfficialMap


OFFICIAL_MAPS = (
    ("gridmap-v2", "Gridmap V2", "/levels/gridmap_v2/info.json"),
    ("west-coast-usa", "West Coast, USA", "/levels/west_coast_usa/info.json"),
    ("east-coast-usa", "East Coast, USA", "/levels/east_coast_usa/info.json"),
    ("johnson-valley", "Johnson Valley", "/levels/johnson_valley/info.json"),
    ("italy", "Italy", "/levels/italy/info.json"),
    ("utah", "Utah, USA", "/levels/utah/info.json"),
    ("jungle-rock-island", "Jungle Rock Island", "/levels/jungle_rock_island/info.json"),
    ("industrial-site", "Industrial Site", "/levels/industrial/info.json"),
    ("hirochi-raceway", "Hirochi Raceway", "/levels/hirochi_raceway/info.json"),
    ("automation-test-track", "Automation Test Track", "/levels/automation_test_track/info.json"),
    ("small-island", "Small Island, USA", "/levels/small_island/info.json"),
    ("derby-arenas", "Derby Arenas", "/levels/derby/info.json"),
)


def official_maps(active_path: str) -> list[OfficialMap]:
    return [
        OfficialMap(id=identifier, name=name, path=path, active=path == active_path)
        for identifier, name, path in OFFICIAL_MAPS
    ]
