"use strict";

window.browserBridge = (() => {
  const pyodideRoot = "https://cdn.jsdelivr.net/pyodide/v0.27.2/full/";
  const modules = ["converter.py", "highscore_bridge.py", "watcher.py", "sample_data_gen.py", "bridge_web.py"];
  const scanExtensions = /\.(hdf5|h5|xrdml|xy|xye|asc|dat|raw)$/i;
  let engine;
  let importNumber = 0;
  let inputLabel = "";

  async function start() {
    if (!window.loadPyodide) {
      await new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = pyodideRoot + "pyodide.js";
        script.onload = resolve;
        script.onerror = () => reject(new Error("Could not load the browser conversion engine. Check your connection and reload."));
        document.head.append(script);
      });
    }
    const py = await loadPyodide({ indexURL: pyodideRoot });
    await Promise.all([
      py.loadPackage(["numpy", "h5py"]),
      ...modules.map(async name => {
        const response = await fetch(name, { cache: "no-cache" });  // always load the current converter after an update
        if (!response.ok) throw new Error(`Could not load ${name} (HTTP ${response.status}).`);
        py.FS.writeFile(name, await response.text());
      }),
    ]);
    return { py, dispatch: py.runPython("from bridge_web import request\nrequest") };
  }

  // Chrome / Edge: open folders read-only and save results straight into a folder, no upload
  // dialog and no ZIP.  Firefox / Safari lack this API and fall back to the file input + ZIP.
  const fsAccess = typeof window.showDirectoryPicker === "function";
  let sourceHandle = null;
  let destHandle = null;
  let written = new Set();
  const notSelected = fsAccess ? "" : "ZIP download (this browser cannot save into folders)";

  function selectFiles(directory) {
    return new Promise(resolve => {
      const input = document.createElement("input");
      input.type = "file";
      input.multiple = true;
      input.hidden = true;
      if (directory) input.webkitdirectory = true;
      else input.accept = ".hdf5,.h5,.xrdml,.xy,.xye,.asc,.dat,.raw";
      const finish = files => {
        input.remove();
        resolve(files.map(file => ({
          parts: file.webkitRelativePath ? file.webkitRelativePath.split("/").slice(1) : [file.name],
          file,
          root: file.webkitRelativePath ? file.webkitRelativePath.split("/")[0] : "",
        })));
      };
      input.onchange = () => finish(Array.from(input.files));
      input.oncancel = () => finish([]);
      document.body.append(input);
      input.click();
    });
  }

  function wanted(parts) {
    const relative = parts.join("/");
    return scanExtensions.test(relative) || /^highscore_results\/.+\.csv$/i.test(relative);
  }

  async function collect(dir, parts, out, depth) {
    for await (const [name, entry] of dir.entries()) {
      if (entry.kind === "directory") {
        if (depth < 8 && !name.startsWith(".")) await collect(entry, [...parts, name], out, depth + 1);
      } else if (wanted([...parts, name])) {
        out.push({ parts: [...parts, name], file: await entry.getFile() });
      }
    }
  }

  async function picked(promise) {
    try {
      return await promise;
    } catch (err) {
      if (err && err.name === "AbortError") return null;  // picker closed
      throw err;
    }
  }

  async function openSource(directory) {
    if (!fsAccess) return selectFiles(directory);
    if (directory) {
      const handle = await picked(window.showDirectoryPicker({ id: "xrd-source", mode: "read" }));
      if (!handle) return [];
      const files = [];
      await collect(handle, [], files, 0);
      sourceHandle = handle;
      written = new Set();
      files.root = handle.name;
      return files;
    }
    const handles = await picked(window.showOpenFilePicker({ multiple: true }));
    if (!handles) return [];
    sourceHandle = null;  // single files: results need a separate output folder
    return Promise.all(handles.map(async h => ({ parts: [h.name], file: await h.getFile() })));
  }

  async function ensureWrite(handle) {
    const opts = { mode: "readwrite" };
    if ((await handle.queryPermission(opts)) === "granted") return;
    if ((await handle.requestPermission(opts)) !== "granted") {
      throw new Error("Permission to save into the folder was not granted.");
    }
  }

  async function prepareSave() {
    if (document.getElementById("outputMode").value === "same_dir") {
      if (!sourceHandle) {
        throw new Error("Same folder needs a raw data folder opened with Browse. For single files or demo data, choose Separate folder.");
      }
      await ensureWrite(sourceHandle);
    } else {
      if (!destHandle) throw new Error("Choose a destination folder first (Browse under Destination).");
      await ensureWrite(destHandle);
    }
  }

  async function madeByBridge(handle) {
    const head = await (await handle.getFile()).slice(0, 4096).arrayBuffer();
    return new TextDecoder("latin1").decode(head).includes("SDL Bridge");
  }

  // Write converted files into the real folder.  An existing file is replaced only if this bridge
  // wrote it (marker in its header, or written earlier in this session); anything else is skipped.
  async function saveOutputs(py, files) {
    let saved = 0;
    const skipped = [];
    for (const item of files) {
      const root = item.root === "input" ? sourceHandle : destHandle;
      if (!root) {
        skipped.push(item.rel);
        continue;
      }
      const parts = item.rel.split("/");
      const name = parts.pop();
      let dir = root;
      for (const part of parts) dir = await dir.getDirectoryHandle(part, { create: true });
      const key = `${item.root}:${item.rel}`;
      let existing = null;
      try {
        existing = await dir.getFileHandle(name);
      } catch (err) {
        if (err.name !== "NotFoundError" && err.name !== "TypeMismatchError") throw err;
        if (err.name === "TypeMismatchError") {
          skipped.push(item.rel);
          continue;
        }
      }
      if (existing && !written.has(key) && !(await madeByBridge(existing))) {
        skipped.push(item.rel);
        continue;
      }
      const handle = existing || (await dir.getFileHandle(name, { create: true }));
      const stream = await handle.createWritable();
      await stream.write(py.FS.readFile(item.path));
      await stream.close();
      written.add(key);
      saved += 1;
    }
    return { saved, skipped };
  }

  async function importFiles(py, files) {
    const root = `/work/imports/${++importNumber}`;
    py.FS.mkdirTree(root);
    for (const { parts, file } of files) {
      if (parts.some(part => !part || part === "." || part === ".." || part.includes("\\"))) {
        throw new Error("The selected file has an unsupported path.");
      }
      if (!wanted(parts)) continue;
      py.FS.mkdirTree(`${root}/${parts.slice(0, -1).join("/")}`);
      py.FS.writeFile(`${root}/${parts.join("/")}`, new Uint8Array(await file.arrayBuffer()));
    }
    return root;
  }

  function downloadZip(py) {
    const url = URL.createObjectURL(new Blob([py.FS.readFile("/work/converted.zip")], { type: "application/zip" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "XRD-converted.zip";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }

  async function setupGuide() {
    const [guide_md, pascal_script] = await Promise.all(
      ["HIGHSCORE_SETUP_GUIDE.md", "auto_search_match_icdd.pas"].map(async name => {
        const response = await fetch(name);
        if (!response.ok) throw new Error(`Could not load ${name}.`);
        return response.text();
      }),
    );
    return { guide_md, pascal_script };
  }

  async function request(url, options) {
    const { pathname, searchParams } = new URL(url, location.origin);
    if (pathname === "/api/setup-guide") return setupGuide();
    const payload = options.body ? JSON.parse(options.body) : Object.fromEntries(searchParams);
    const converting = pathname === "/api/convert-all" || pathname === "/api/convert-one";
    if (converting && fsAccess) await prepareSave();  // ask for write access while the click still counts
    let files;
    let pickedOutput = false;
    if (pathname === "/api/browse-folder") {
      if (payload.target === "custom_output_dir") {
        if (!fsAccess) throw new Error("This browser cannot save into folders. Use Chrome or Edge, or the ZIP download.");
        const handle = await picked(window.showDirectoryPicker({ id: "xrd-output", mode: "readwrite" }));
        if (handle) {
          destHandle = handle;
          written = new Set();
          pickedOutput = true;
        }
      } else {
        files = await openSource(payload.target !== "input_files");
        if (files.length && !files.some(f => scanExtensions.test(f.parts[f.parts.length - 1]))) {
          throw new Error("The selection contains no supported XRD files.");
        }
      }
    }
    engine ??= start();
    const { py, dispatch } = await engine;
    let route = pathname;
    if (files?.length) payload.path = await importFiles(py, files);
    else if (files || pathname === "/api/browse-folder") route = "/api/state";
    if (converting) payload.zip = !fsAccess;
    let response = JSON.parse(dispatch(route, JSON.stringify(payload)));
    if (response.error) throw new Error(response.error);
    if (files?.length) {
      inputLabel = files.root || files[0].root || `${files.length} selected file(s)`;
      response.path = inputLabel;
    } else if (pickedOutput) {
      response = { ok: true, path: destHandle.name, state: response };
    } else if (route === "/api/state" && pathname === "/api/browse-folder") {
      response = { ok: false, state: response };
    } else if (route === "/api/generate-demo") {
      sourceHandle = null;
      inputLabel ||= "Demo data";
    }
    const state = response.state || (route === "/api/state" ? response : null);
    if (state) {
      state.config.input_dir = inputLabel;
      state.config.custom_output_dir = destHandle ? destHandle.name : notSelected;
    }
    if (response.download) downloadZip(py);
    if (converting && fsAccess && response.files) {
      const { saved, skipped } = await saveOutputs(py, response.files);
      const where = document.getElementById("outputMode").value === "same_dir" ? sourceHandle.name : destHandle.name;
      let note = `Saved ${saved} file(s) to ${where}.`;
      if (skipped.length) note += ` Skipped ${skipped.length} existing file(s) not made by the bridge: ${skipped.slice(0, 3).join(", ")}${skipped.length > 3 ? "..." : ""}`;
      setTimeout(() => window.showToast && window.showToast(note), 1500);
    }
    return response;
  }

  function configure() {
    const source = document.getElementById("inputDir");
    source.readOnly = true;
    const files = document.createElement("button");
    files.type = "button";
    files.textContent = "Choose files";
    files.onclick = () => browseFolder("input_files");
    source.parentElement.append(files);
    const dest = document.getElementById("customOutputDir");
    dest.readOnly = true;
    dest.placeholder = fsAccess ? "Choose an output folder..." : notSelected;
    let desktopOnly = "#watchBtn, #highscoreExe, #autoLaunch, [onclick='browseExe()']";
    if (!fsAccess) desktopOnly += ", #customOutWrap button, #outputMode";
    document.querySelectorAll(desktopOnly).forEach(control => {
      control.disabled = true;
      control.title = "Available in the desktop app";
    });
    document.querySelector(".run-note").textContent = fsAccess
      ? "Files stay on this device; nothing is uploaded. Results are saved straight into the folder you choose. Live watch is available in the desktop app."
      : "Files stay on this device; nothing is uploaded to a server. This browser cannot open or save folders directly, so results download as a ZIP. Use Chrome or Edge to save into folders.";
  }

  return { configure, request };
})();
