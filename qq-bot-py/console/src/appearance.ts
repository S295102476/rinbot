import { useCallback, useEffect, useRef, useState } from "react";
import starfield from "./assets/wallpapers/starfield.svg";
import nebula from "./assets/wallpapers/nebula.svg";
import aurora from "./assets/wallpapers/aurora.svg";

export const wallpapers = [
  { id: "starfield", name: "深空星河", description: "星光与远山，安静的深蓝夜色", src: starfield },
  { id: "nebula", name: "紫雾星云", description: "柔和的紫色星云与玫瑰色微光", src: nebula },
  { id: "aurora", name: "极光山谷", description: "青绿色极光，漫过山谷与天际", src: aurora },
] as const;

export const accents = [
  { id: "blue", name: "海洋蓝", light: "#3668d5", dark: "#89afff", rgb: "54, 104, 213" },
  { id: "rose", name: "樱花粉", light: "#b83b67", dark: "#f1a2c0", rgb: "184, 59, 103" },
  { id: "purple", name: "星云紫", light: "#8650c8", dark: "#c6a4ff", rgb: "134, 80, 200" },
  { id: "teal", name: "森林绿", light: "#177a68", dark: "#76d6bd", rgb: "23, 122, 104" },
  { id: "amber", name: "日落橙", light: "#b46314", dark: "#ffc180", rgb: "180, 99, 20" },
] as const;

export type Appearance = {
  mode: "light" | "dark" | "system";
  surface: "solid" | "glass";
  wallpaper: "none" | "custom" | typeof wallpapers[number]["id"];
  accent: typeof accents[number]["id"];
  opacity: number;
  blur: number;
  dim: number;
};

export const defaultAppearance: Appearance = {
  mode: "system", surface: "glass", wallpaper: "starfield", accent: "blue",
  opacity: 88, blur: 18, dim: 25,
};
const storageKey = "qq-admin-appearance";

function normalize(value: unknown): Appearance {
  const raw = value && typeof value === "object" ? value as Partial<Appearance> : {};
  const number = (key: "opacity" | "blur" | "dim", min: number, max: number) =>
    typeof raw[key] === "number" && Number.isFinite(raw[key])
      ? Math.max(min, Math.min(max, raw[key]!)) : defaultAppearance[key];
  return {
    mode: raw.mode === "dark" || raw.mode === "light" ? raw.mode : "system",
    surface: raw.surface === "solid" ? "solid" : "glass",
    wallpaper: raw.wallpaper === "none" || raw.wallpaper === "custom" || wallpapers.some(w => w.id === raw.wallpaper)
      ? raw.wallpaper! : defaultAppearance.wallpaper,
    accent: accents.some(c => c.id === raw.accent) ? raw.accent! : defaultAppearance.accent,
    opacity: number("opacity", 60, 96), blur: number("blur", 0, 28), dim: number("dim", 0, 70),
  };
}

function readPreferences(): Appearance {
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved) return normalize(JSON.parse(saved));
    const legacy = localStorage.getItem("qq-admin-theme");
    return { ...defaultAppearance, mode: legacy === "dark" || legacy === "light" ? legacy : "system" };
  } catch { return { ...defaultAppearance }; }
}

async function wallpaperStore(action: "read" | "write" | "delete", blob?: Blob): Promise<Blob | null> {
  const database = await new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open("rinbot-console-appearance", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("wallpapers");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error("浏览器无法打开壁纸存储，请检查站点存储权限。"));
    request.onblocked = () => reject(new Error("壁纸存储被其他标签页占用，请关闭旧标签页后重试。"));
  });
  try {
    return await new Promise<Blob | null>((resolve, reject) => {
      const tx = database.transaction("wallpapers", action === "read" ? "readonly" : "readwrite");
      const store = tx.objectStore("wallpapers");
      const request = action === "read" ? store.get("custom")
        : action === "write" ? store.put(blob, "custom") : store.delete("custom");
      tx.oncomplete = () => resolve(action === "read" && request.result instanceof Blob ? request.result : null);
      tx.onabort = tx.onerror = () => reject(new Error("壁纸保存失败，请检查浏览器可用空间与站点存储权限。"));
    });
  } finally { database.close(); }
}

function imageDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error("图片无法读取，请重新选择文件。"));
    reader.readAsDataURL(blob);
  });
}

async function prepareWallpaper(file: File): Promise<Blob> {
  if (!["image/jpeg", "image/png", "image/webp"].includes(file.type))
    throw new Error("请选择 JPG、PNG 或 WebP 图片。");
  if (file.size > 15 * 1024 ** 2) throw new Error("图片不能超过 15 MiB，请先压缩后重试。");
  // The console CSP allows data images; keep local previews within that policy.
  const url = await imageDataUrl(file);
  const image = new Image();
  try {
    image.src = url;
    await image.decode().catch(() => { throw new Error("图片无法读取，请选择有效的图片文件。"); });
    if (!image.naturalWidth || !image.naturalHeight) throw new Error("图片尺寸无效。");
    const scale = Math.min(1, 2560 / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    const context = canvas.getContext("2d");
    if (!context) throw new Error("浏览器不支持图片处理。");
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    return await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error("图片处理失败。")), "image/webp", 0.88);
    });
  } finally { image.src = ""; }
}

export function useAppearance() {
  const [appearance, setPreferences] = useState<Appearance>(readPreferences);
  const preferenceRef = useRef(appearance);
  const [systemDark, setSystemDark] = useState(() => matchMedia("(prefers-color-scheme: dark)").matches);
  const [customBlob, setCustomBlob] = useState<Blob | null>(null);
  const [customUrl, setCustomUrl] = useState("");
  const [storageError, setStorageError] = useState("");
  const theme = appearance.mode === "system" ? (systemDark ? "dark" : "light") : appearance.mode;

  const setAppearance = useCallback((patch: Partial<Appearance>) => {
    const next = normalize({ ...preferenceRef.current, ...patch });
    try {
      localStorage.setItem(storageKey, JSON.stringify(next));
      setStorageError("");
    } catch {
      setStorageError("浏览器无法保存外观设置，本次调整仅在当前页面生效。");
    }
    preferenceRef.current = next;
    setPreferences(next);
  }, []);

  useEffect(() => {
    const media = matchMedia("(prefers-color-scheme: dark)");
    const update = () => setSystemDark(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    let active = true;
    wallpaperStore("read").then(blob => { if (active) setCustomBlob(blob); }).catch(() => {
      if (active && preferenceRef.current.wallpaper === "custom")
        setStorageError("无法读取自定义壁纸，已暂用默认星空。请重新选择图片。");
    });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    if (!customBlob) setCustomUrl("");
    else imageDataUrl(customBlob).then(url => { if (active) setCustomUrl(url); }).catch(() => {
      if (active) setStorageError("自定义壁纸预览失败，请重新选择图片。");
    });
    return () => { active = false; };
  }, [customBlob]);

  useEffect(() => {
    const root = document.documentElement;
    const accent = accents.find(c => c.id === appearance.accent)!;
    root.dataset.theme = theme;
    root.dataset.surface = appearance.surface;
    root.dataset.wallpaper = appearance.wallpaper;
    root.style.setProperty("--rose", theme === "dark" ? accent.dark : accent.light);
    root.style.setProperty("--accent-rgb", accent.rgb);
    root.style.setProperty("--panel-opacity", String(appearance.opacity / 100));
    root.style.setProperty("--glass-blur", `${appearance.blur}px`);
    root.style.setProperty("--wallpaper-dim", String(appearance.dim / 100));
  }, [appearance, theme]);

  const uploadWallpaper = useCallback(async (file: File) => {
    const blob = await prepareWallpaper(file);
    await wallpaperStore("write", blob);
    setCustomBlob(blob);
    setAppearance({ wallpaper: "custom" });
  }, [setAppearance]);

  const removeCustomWallpaper = useCallback(async () => {
    await wallpaperStore("delete");
    setCustomBlob(null);
    if (preferenceRef.current.wallpaper === "custom") setAppearance({ wallpaper: "starfield" });
  }, [setAppearance]);

  const wallpaperUrl = appearance.wallpaper === "none" ? ""
    : appearance.wallpaper === "custom" ? customUrl || starfield
    : wallpapers.find(w => w.id === appearance.wallpaper)!.src;

  return {
    appearance, theme, setAppearance, wallpaperUrl, uploadWallpaper, removeCustomWallpaper,
    hasCustomWallpaper: !!customBlob, customUrl, storageError,
    resetAppearance: () => setAppearance(defaultAppearance),
  };
}

export type AppearanceController = ReturnType<typeof useAppearance>;
