(function () {
  function bindFilePreview(input) {
    const previewId = input.getAttribute("data-preview");
    const preview = previewId
      ? document.getElementById(previewId)
      : input.closest("label")?.querySelector(".file-name");

    if (!preview) return;

    input.addEventListener("change", () => {
      const files = Array.from(input.files || []);
      if (!files.length) {
        preview.textContent = input.multiple ? "No files selected" : "No file selected";
        return;
      }
      if (files.length === 1) {
        preview.textContent = files[0].name;
      } else {
        preview.textContent = `${files.length} files · ${files.map((f) => f.name).join(", ")}`;
      }
    });
  }

  document.querySelectorAll('input[type="file"]').forEach(bindFilePreview);

  const masterInput = document.getElementById("master_file");
  const masterName = document.getElementById("master-file-name");
  if (masterInput && masterName) {
    masterInput.addEventListener("change", () => {
      masterName.textContent = masterInput.files?.[0]?.name || "No file selected";
    });
  }

  const dropzone = document.getElementById("master-dropzone");
  if (dropzone && masterInput) {
    ["dragenter", "dragover"].forEach((evt) => {
      dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        dropzone.classList.add("is-dragover");
      });
    });
    ["dragleave", "drop"].forEach((evt) => {
      dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        dropzone.classList.remove("is-dragover");
      });
    });
  }

  // Auto-dismiss flash messages
  document.querySelectorAll(".flash").forEach((el) => {
    setTimeout(() => {
      el.style.transition = "opacity 0.35s ease, transform 0.35s ease";
      el.style.opacity = "0";
      el.style.transform = "translateY(-6px)";
      setTimeout(() => el.remove(), 400);
    }, 4500);
  });
})();
