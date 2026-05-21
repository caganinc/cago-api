/**
 * CAGO 3D — Kalibrasyon API istemcisi
 * Mevcut React sitenin admin paneline eklenecek JavaScript parçacığı.
 *
 * Kullanım:
 *   import { getCalibration, submitSiteEstimate, getRecentBridgeEvents } from './calibration.js'
 */

const API_BASE = "https://cago-api-production.up.railway.app";
const API_KEY  = "";                         // Boş bırakılırsa doğrulama yapılmaz

const headers = () => ({
  "Content-Type": "application/json",
  ...(API_KEY ? { Authorization: `Bearer ${API_KEY}` } : {}),
});

/**
 * Tüm kalibrasyon katsayılarını çek.
 * Admin paneli açılışında çağır → localStorage'a yaz.
 *
 * @returns {{ calibrations: Array<{ profile_key, k_weight, k_time, sample_count }> }}
 */
export async function getAllCalibrations() {
  const res = await fetch(`${API_BASE}/v1/calibrations`, { headers: headers() });
  if (!res.ok) throw new Error(`API hatası: ${res.status}`);
  return res.json();
}

/**
 * Belirli bir profil için k_weight ve k_time al.
 * Örnek: getCalibration("PLA_0.2_15pct")
 *
 * @param {string} profileKey
 * @returns {{ k_weight: number, k_time: number, sample_count: number }}
 */
export async function getCalibration(profileKey) {
  const res = await fetch(`${API_BASE}/v1/calibrations/${profileKey}`, { headers: headers() });
  if (!res.ok) throw new Error(`API hatası: ${res.status}`);
  return res.json();
}

/**
 * Admin paneli: sitenin kendi tahminini göndererek kalibrasyon yap.
 * "Karşılaştır" butonuna basılınca çağrılır.
 *
 * @param {string} profileKey   - örn: "PLA_0.20_15pct"
 * @param {number} siteWeightG  - sitenin tahmini gram
 * @param {number} siteTimeMin  - sitenin tahmini dakika
 */
export async function submitSiteEstimate(profileKey, siteWeightG, siteTimeMin) {
  const res = await fetch(`${API_BASE}/v1/calibrate`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({
      profile_key:   profileKey,
      site_weight_g: siteWeightG,
      site_time_min: siteTimeMin,
    }),
  });
  if (!res.ok) throw new Error(`API hatası: ${res.status}`);
  return res.json();
  // Dönüş: { new_k_weight, new_k_time, bambu_weight_g, bambu_time_min, ... }
}

/**
 * Son bridge slice olaylarını listele (admin paneli için).
 *
 * @param {number} limit
 */
export async function getRecentBridgeEvents(limit = 20) {
  const res = await fetch(`${API_BASE}/v1/slice-events?limit=${limit}`, { headers: headers() });
  if (!res.ok) throw new Error(`API hatası: ${res.status}`);
  return res.json();
}

/**
 * k değerlerini localStorage'a yaz (site kendi store'una aktarır).
 * Mevcut admin store'undaki setCalibration fonksiyonunu çağır.
 *
 * Örnek kullanım (admin paneli açılışında):
 *   syncCalibrationsToStore(adminStore.setCalibration)
 */
export async function syncCalibrationsToStore(setCalibrationFn) {
  try {
    const { calibrations } = await getAllCalibrations();
    calibrations.forEach(c => {
      setCalibrationFn(c.profile_key, {
        k_weight:     c.k_weight,
        k_time:       c.k_time,
        sample_count: c.sample_count,
        last_updated: c.last_updated,
      });
    });
    return { synced: calibrations.length };
  } catch (err) {
    console.warn("Kalibrasyon API erişilemedi:", err.message);
    return { synced: 0, error: err.message };
  }
}
