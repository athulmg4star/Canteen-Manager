(() => {
  const defaults={theme:'system',density:'comfortable',sidebar:'expanded',motion:'system'};
  let saved={};try{saved=JSON.parse(localStorage.getItem('tiffin.preferences')||'{}');}catch{}
  window.tiffinPreferences={...defaults,...saved};
  window.applyTiffinPreferences=()=>{const p=window.tiffinPreferences,r=document.documentElement;r.dataset.theme=p.theme==='system'?(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'):p.theme;r.dataset.density=p.density;r.dataset.sidebar=p.sidebar;r.dataset.motion=p.motion;};
  window.saveTiffinPreferences=()=>{try{localStorage.setItem('tiffin.preferences',JSON.stringify(window.tiffinPreferences));}catch{}window.applyTiffinPreferences();window.dispatchEvent(new Event('preferenceschange'));};
  applyTiffinPreferences();matchMedia('(prefers-color-scheme: dark)').addEventListener('change',applyTiffinPreferences);
})();
