(() => {
  const BEAM_MAP_THUMBNAILS_V1 = true;
  if (typeof contentVisual !== "function") return;

  const baseContentVisual = contentVisual;
  const roots = ["/map-thumbnails", "/assets/map-thumbnails"];
  const extensions = [".webp", ".jpg", ".jpeg", ".png"];

  function officialLevelId(item) {
    if (!item?.official) return null;
    const path = String(item.path || "").replaceAll("\\", "/");
    const match = path.match(/^\/?levels\/([^/]+)\/info\.json$/i);
    return match?.[1] || null;
  }

  function attachOfficialThumbnail(visual, item) {
    if (visual.querySelector("img")) return;
    const levelId = officialLevelId(item);
    if (!levelId) return;

    const image = document.createElement("img");
    image.alt = "";
    image.loading = "lazy";
    let rootIndex = 0;
    let extensionIndex = 0;

    const tryNext = () => {
      if (extensionIndex >= extensions.length) {
        rootIndex += 1;
        extensionIndex = 0;
      }
      if (rootIndex >= roots.length) {
        image.remove();
        return;
      }
      const extension = extensions[extensionIndex++];
      image.src = `${roots[rootIndex]}/${encodeURIComponent(levelId)}${extension}`;
    };

    image.addEventListener("error", tryNext);
    tryNext();
    visual.prepend(image);
  }

  contentVisual = function enhancedContentVisual(item, kind) {
    const visual = baseContentVisual(item, kind);
    if (kind === "map") attachOfficialThumbnail(visual, item);
    return visual;
  };

  void BEAM_MAP_THUMBNAILS_V1;
})();
