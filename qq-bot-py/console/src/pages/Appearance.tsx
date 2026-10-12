import { useState, type ReactNode } from "react";
import { Check, Image, Layers3, Moon, Palette, RotateCcw, SlidersHorizontal, Sparkles, Sun, Upload } from "lucide-react";
import { accents, defaultAppearance, wallpapers, type Appearance as Preferences, type AppearanceController } from "../appearance";
import { Notice } from "../ui";
import { PageHeading } from "./Operations";

function Choice({ name, hint, selected, onClick, children }: {
  name: string; hint?: string; selected: boolean; onClick: () => void; children?: ReactNode;
}) {
  return <button type="button" className={`appearance-choice ${selected ? "selected" : ""}`}
    aria-label={name} aria-pressed={selected} onClick={onClick}>
    {selected && <Check className="choice-check" size={16} />}
    {children}<strong>{name}</strong>{hint && <small>{hint}</small>}
  </button>;
}

function Section({ title, hint, icon, children, className = "" }: {
  title: string; hint: string; icon: ReactNode; children: ReactNode; className?: string;
}) {
  return <section className={`card appearance-card ${className}`}>
    <div className="appearance-card-heading"><span>{icon}</span><div><h2>{title}</h2><p>{hint}</p></div></div>
    {children}
  </section>;
}

const presets: Array<{ name: string; hint: string; settings: Preferences }> = [
  { name: "星河夜航", hint: "深蓝星空 · 通透玻璃 · 海洋蓝", settings: { ...defaultAppearance, mode: "dark", opacity: 80, dim: 20 } },
  { name: "星云漫游", hint: "紫色星云 · 柔和玻璃 · 星云紫", settings: { ...defaultAppearance, mode: "dark", wallpaper: "nebula", accent: "purple", opacity: 78, dim: 25 } },
  { name: "极光微光", hint: "青绿极光 · 沉静玻璃 · 森林绿", settings: { ...defaultAppearance, mode: "dark", wallpaper: "aurora", accent: "teal", opacity: 82, dim: 20 } },
  { name: "简洁日光", hint: "浅色界面 · 纯色面板 · 无壁纸", settings: { ...defaultAppearance, mode: "light", surface: "solid", wallpaper: "none", accent: "blue" } },
];

export function Appearance({ controller }: { controller: AppearanceController }) {
  const { appearance, setAppearance, wallpaperUrl, hasCustomWallpaper, customUrl } = controller;
  const [tab, setTab] = useState("appearance");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  async function upload(file?: File) {
    if (!file) return;
    setBusy(true); setError(""); setMessage("");
    try { await controller.uploadWallpaper(file); setMessage("自定义壁纸已保存。"); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "壁纸保存失败。"); }
    finally { setBusy(false); }
  }
  async function remove() {
    setBusy(true); setError(""); setMessage("");
    try { await controller.removeCustomWallpaper(); setMessage("自定义壁纸已移除。"); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "壁纸删除失败。"); }
    finally { setBusy(false); }
  }
  return <div className="appearance-page">
    <PageHeading title="外观设置" subtitle="让工作区拥有自己的夜空。调整即时生效，并自动保存在当前浏览器。">
      <button className="button" onClick={() => { controller.resetAppearance(); setMessage("已恢复默认外观。"); }}><RotateCcw size={15} />恢复默认</button>
    </PageHeading>
    <div className="appearance-hero card">
      <div><span className="eyebrow"><Sparkles size={14} /> YOUR SPACE, YOUR SKY</span><h2>一方星空，一处工作区</h2><p>壁纸、色彩与玻璃质感，由你决定。</p></div>
      <div className="appearance-mini-preview" aria-hidden="true" style={{ backgroundImage: wallpaperUrl ? `url("${wallpaperUrl}")` : undefined }}>
        <i /><div><span /><span /><section><b /><b /><b /></section></div>
      </div>
    </div>
    <div className="appearance-tabs" aria-label="外观设置分类">
      {[{ id: "appearance", name: "外观", icon: Palette }, { id: "background", name: "背景", icon: Image }, { id: "presets", name: "主题预设", icon: Layers3 }].map(item =>
        <button key={item.id} className={tab === item.id ? "active" : ""} aria-pressed={tab === item.id} onClick={() => setTab(item.id)}><item.icon size={17} />{item.name}</button>)}
      <span><Check size={14} />自动保存</span>
    </div>
    <Notice>{error || controller.storageError}</Notice><Notice kind="success">{message}</Notice>
    {tab === "appearance" && <div className="appearance-grid">
      <Section title="颜色模式" hint="选择白昼、夜色，或跟随系统切换。" icon={<Moon size={20} />}>
        <div className="appearance-options three">
          <Choice name="浅色模式" selected={appearance.mode === "light"} onClick={() => setAppearance({ mode: "light" })}><Sun size={25} /></Choice>
          <Choice name="深色模式" selected={appearance.mode === "dark"} onClick={() => setAppearance({ mode: "dark" })}><Moon size={25} /></Choice>
          <Choice name="跟随系统" selected={appearance.mode === "system"} onClick={() => setAppearance({ mode: "system" })}><SlidersHorizontal size={25} /></Choice>
        </div>
      </Section>
      <Section title="界面质感" hint="纯色更专注，毛玻璃让壁纸柔和地透过面板。" icon={<Layers3 size={20} />}>
        <div className="appearance-options">
          <Choice name="纯色风格" selected={appearance.surface === "solid"} onClick={() => setAppearance({ surface: "solid" })}><span className="surface-sample solid" /></Choice>
          <Choice name="毛玻璃风格" selected={appearance.surface === "glass"} onClick={() => setAppearance({ surface: "glass" })}><span className="surface-sample glass" /></Choice>
        </div>
      </Section>
      <Section className="appearance-span" title="主题颜色" hint="导航、按钮和焦点使用同一套强调色。" icon={<Palette size={20} />}>
        <div className="accent-options">{accents.map(color => <button key={color.id} aria-label={color.name} aria-pressed={appearance.accent === color.id}
          className={`accent-choice ${appearance.accent === color.id ? "selected" : ""}`} onClick={() => setAppearance({ accent: color.id })}>
          <span style={{ background: color.light }}>{appearance.accent === color.id && <Check size={20} />}</span><small>{color.name}</small>
        </button>)}</div>
      </Section>
      <Section title="面板不透明度" hint="提高不透明度，可以让复杂背景上的文字更清晰。" icon={<SlidersHorizontal size={20} />}>
        <label className="appearance-slider" htmlFor="appearance-opacity"><span>面板不透明度<output htmlFor="appearance-opacity">{appearance.opacity}%</output></span><input id="appearance-opacity" aria-label="面板不透明度" type="range" min="60" max="96" value={appearance.opacity} disabled={appearance.surface === "solid"} onChange={e => setAppearance({ opacity: Number(e.target.value) })} /></label>
        {appearance.surface === "solid" && <p className="appearance-hint">纯色模式保持面板完全不透明。</p>}
      </Section>
      <Section title="毛玻璃模糊" hint="适当模糊背景，保留光影而淡化图片细节。" icon={<Sparkles size={20} />}>
        <label className="appearance-slider" htmlFor="appearance-blur"><span>毛玻璃模糊<output htmlFor="appearance-blur">{appearance.blur} px</output></span><input id="appearance-blur" aria-label="毛玻璃模糊" type="range" min="0" max="28" value={appearance.blur} disabled={appearance.surface === "solid"} onChange={e => setAppearance({ blur: Number(e.target.value) })} /></label>
      </Section>
    </div>}
    {tab === "background" && <div className="appearance-grid">
      <Section className="appearance-span" title="默认壁纸" hint="三张随控制台打包的原创星空背景，无需连接外部图床。" icon={<Image size={20} />}>
        <div className="wallpaper-options">
          <Choice name="无壁纸" hint="简洁纯色背景" selected={appearance.wallpaper === "none"} onClick={() => setAppearance({ wallpaper: "none" })}><span className="wallpaper-thumbnail no-wallpaper"><Sun size={28} /></span></Choice>
          {wallpapers.map(w => <Choice key={w.id} name={w.name} hint={w.description} selected={appearance.wallpaper === w.id} onClick={() => setAppearance({ wallpaper: w.id })}><img className="wallpaper-thumbnail" src={w.src} alt="" /></Choice>)}
        </div>
      </Section>
      <Section title="自定义壁纸" hint="选择本地图片，刷新后依然保留。" icon={<Upload size={20} />}>
        {hasCustomWallpaper && <button className={`custom-wallpaper-preview ${appearance.wallpaper === "custom" ? "selected" : ""}`} aria-label="使用自定义壁纸" aria-pressed={appearance.wallpaper === "custom"} onClick={() => setAppearance({ wallpaper: "custom" })}><img src={customUrl} alt="已保存的自定义壁纸" />{appearance.wallpaper === "custom" && <span><Check size={14} />正在使用</span>}</button>}
        <label className="wallpaper-upload"><Upload size={25} /><strong>{busy ? "正在处理图片…" : hasCustomWallpaper ? "更换自定义壁纸" : "选择一张喜欢的图片"}</strong><span>JPG / PNG / WebP，最大 15 MiB</span><input aria-label="选择自定义壁纸" type="file" accept="image/jpeg,image/png,image/webp" disabled={busy} onChange={e => { void upload(e.target.files?.[0]); e.target.value = ""; }} /></label>
        <p className="appearance-hint">图片保存在当前浏览器，自动缩放到最长边 2560px。换浏览器或清除站点数据后，需要重新选择。</p>
        {hasCustomWallpaper && <button className="button" disabled={busy} onClick={() => void remove()}>移除自定义壁纸</button>}
      </Section>
      <Section title="壁纸遮罩" hint="调暗背景，让内容始终清楚。" icon={<SlidersHorizontal size={20} />}>
        <label className="appearance-slider" htmlFor="appearance-dim"><span>壁纸遮罩<output htmlFor="appearance-dim">{appearance.dim}%</output></span><input id="appearance-dim" aria-label="壁纸遮罩" type="range" min="0" max="70" value={appearance.dim} onChange={e => setAppearance({ dim: Number(e.target.value) })} /></label>
        <div className="wallpaper-current" style={{ backgroundImage: wallpaperUrl ? `url("${wallpaperUrl}")` : undefined }}><span style={{ background: `rgba(3, 8, 20, ${appearance.dim / 100})` }} /><p>当前背景预览</p></div>
      </Section>
    </div>}
    {tab === "presets" && <Section title="主题预设" hint="一键应用配好的背景、质感与颜色，再按喜好微调。" icon={<Layers3 size={20} />}>
      <div className="theme-presets">{presets.map(preset => {
        const background = wallpapers.find(w => w.id === preset.settings.wallpaper);
        return <button key={preset.name} className="theme-preset" onClick={() => { setAppearance(preset.settings); setMessage(`已应用「${preset.name}」。`); }}>
          <span className={`preset-thumbnail ${preset.settings.mode}`} style={{ backgroundImage: background ? `url("${background.src}")` : undefined }}><i /><b /><b /><b /></span><strong>{preset.name}</strong><small>{preset.hint}</small>
        </button>;
      })}</div>
    </Section>}
  </div>;
}
