# Kurulum ve İşletme Kılavuzu

Bu doküman sistemi Railway üzerinde sıfırdan ayağa kaldırmayı anlatır.
Kod ve commit'ler İngilizce; bu kılavuz operasyon içindir.

---

## 1. Ön hazırlık

Gerekenler:

- **GitHub** hesabı (repo Railway'e bağlanacak)
- **Railway** hesabı — Postgres + Redis eklentileri ücretsiz katmanda yeterli
- **Telegram bot token'ı** — [@BotFather](https://t.me/BotFather) → `/newbot`

Ücretli bir API anahtarı **gerekmiyor**. Sistem public RPC + DexScreener +
GeckoTerminal ücretsiz katmanlarıyla çalışacak şekilde tasarlandı.

---

## 2. Railway projesi

1. Railway'de **New Project → Deploy from GitHub repo** ile bu repoyu seç.
2. Projeye **PostgreSQL** eklentisini ekle (`+ New → Database → PostgreSQL`).
3. Projeye **Redis** eklentisini ekle.

### Ortak değişkenler

Proje ayarlarında (`Variables → Shared Variables`) şunları bir kez tanımla;
üç servis de miras alır:

```
DATABASE_URL           = ${{Postgres.DATABASE_URL}}
REDIS_URL              = ${{Redis.REDIS_URL}}
TELEGRAM_BOT_TOKEN     = BotFather'dan aldığın token
TELEGRAM_ADMIN_IDS     = kendi Telegram sayısal ID'in
ENVIRONMENT            = production
LOG_LEVEL              = INFO
ENABLED_CHAINS         = base,robinhood,bsc
```

> Telegram sayısal ID'ini öğrenmek için [@userinfobot](https://t.me/userinfobot)'a
> yazabilirsin.

---

## 3. Üç servis

Aynı imajdan üç servis oluştur; sadece başlangıç komutları farklı.

| Servis | Start command | Public domain |
|---|---|---|
| `api` | `python scripts/migrate.py && python -m uvicorn coinfinder.api.main:app --host 0.0.0.0 --port $PORT` | **Evet** |
| `worker` | `python -m coinfinder.worker.main` | Hayır |
| `bot` | `python -m coinfinder.bot.main` | Hayır |

Her servis için: `Settings → Deploy → Custom Start Command` alanına yukarıdaki
komutu yaz.

Sadece `api` servisine domain ver (`Settings → Networking → Generate Domain`).
`worker` ve `bot` yalnızca dışarı bağlantı kuruyor, gelen trafik almıyorlar.

Domain'i aldıktan sonra ortak değişkenlere ekle:

```
PUBLIC_BASE_URL = https://senin-app.up.railway.app
```

---

## 4. İlk çalıştırma — ne bekleyeceksin

Sistem ilk deploy'da **hemen sinyal atmaz**. Sırasıyla şu olur:

| Aşama | Süre | Ne oluyor |
|---|---|---|
| Migration | saniyeler | Tablolar kuruluyor |
| **Keşif** (`discover`) | ~1 saat | Son kazanan tokenlerin erken alıcıları çıkarılıyor, aday cüzdanlar toplanıyor |
| **İzleme** (`watch`) | sürekli | Aday cüzdanların işlemleri indeksleniyor |
| **Skorlama** (`rescore`) | 6 saatte bir | Yeterli işlem geçmişi birikince cüzdanlar skorlanıyor, izleme listesi güncelleniyor |
| **Sinyal** | skorlamadan sonra | Konfluans tespiti başlıyor |

Gerçekçi beklenti: **anlamlı sinyaller için 2-4 gün**. Cüzdan skorlaması
minimum 8 kapanmış işlem istiyor (`SMART_WALLET_MIN_TRADES`); o kadar geçmiş
birikene kadar sistem sessiz kalır. Bu bir hata değil, tasarım — az veriyle
skorlanmış cüzdan gürültüden ibarettir.

Beklerken paneli görmek istersen demo veriyi tohumlayabilirsin:

```bash
railway run python scripts/seed_demo.py --yes --signals 1500 --days 75
```

Bu **açıkça sahte** veri yazar. Gerçek sinyaller gelmeye başlayınca tabloları
temizlemeyi unutma.

---

## 5. Sağlık kontrolü

```bash
curl https://senin-app.up.railway.app/health
curl https://senin-app.up.railway.app/api/stats
```

`/api/stats` çıktısında bakılacaklar:

- `watched_wallets` — 0'sa keşif henüz cüzdan bulmamış
- `trades_total` — 0'sa indeksleme çalışmıyor, `worker` loglarına bak
- `last_trade_at` — saatlerdir güncellenmiyorsa RPC sağlayıcıları düşmüş olabilir
- `signals_24h` — sistem üretken mi

Telegram'da `/status` komutu aynı bilgiyi verir.

---

## 6. Ayarlama

Sinyal az geliyorsa (ortak değişkenlerden):

```
CONFLUENCE_MIN_CLUSTERS = 2     # varsayılan 3 — daha çok sinyal, daha çok gürültü
CONFLUENCE_WINDOW_MINUTES = 360 # varsayılan 180
MIN_LIQUIDITY_USD = 3000        # varsayılan 5000
```

Sinyal çok geliyorsa tersini yap. `SIGNAL_COOLDOWN_MINUTES` aynı token için
tekrar alert aralığını belirler (varsayılan 360).

RPC hız limitine takılıyorsan (`worker` loglarında çok sayıda 429):

```
POLL_INTERVAL_SECONDS = 20      # varsayılan 12
LOG_RANGE_BLOCKS = 250          # varsayılan 500
WALLET_WATCH_BATCH = 80         # varsayılan 120
```

Ücretli sağlayıcıya geçmek istersen **kod değişikliği gerekmez**:

```
BASE_RPC_URLS = https://base-mainnet.g.alchemy.com/v2/KEY
BSC_RPC_URLS  = https://bsc-mainnet.g.alchemy.com/v2/KEY
```

---

## 7. Bilinen sınırlar

Bunları bilerek kurduk; sürpriz olmasın diye yazıyorum.

**Robinhood Chain'de risk analizi yok.** Bu zincir için üçüncü parti honeypot/
vergi taraması yok. Sistem bu zincirde asla "safe" demiyor, her alert'e açık
DYOR uyarısı koyuyor. Referans üründeki davranışın aynısı — çünkü başka
dürüst seçenek yok.

**Honeypot tespiti dolaylı.** Gerçek honeypot simülasyonu `eth_call` state
override ister; ücretsiz endpoint'lerin çoğu buna izin vermiyor. Onun yerine
en güçlü ücretsiz sinyali kullanıyoruz: **birileri satabilmiş mi?** Çok alım +
sıfır satım klasik honeypot parmak izidir. LP burn oranı ve ownership de
kontrol ediliyor. Bu iyi bir yaklaşım ama tam simülasyon değil.

**Çoklu-varlık işlemlerinde USD değeri yok.** Bir aggregator tek ödemeyi iki
tokene bölmüşse, fiyat oracle'ı olmadan hangi tokene ne kadar düştüğü
bilinemez. O işlemler PnL'e **dahil edilmiyor** (tahmin etmek yerine atlıyoruz).
Az sayıda işlem kaybediyoruz, karşılığında cüzdan skorları bozulmuyor.

**Kalite modeli başta bir önsel (prior).** İlk 400 sinyal sonuçlanana kadar
`P(2x)` elle konmuş katsayılardan geliyor ve `is_fitted=False` olarak
işaretleniyor. Veri birikince model gerçek sonuçlara fit ediliyor. Panelde
önsel tabanlı sayıları ölçülmüş performans gibi sunma.

**Backtest yalnızca kendi sinyallerini görür.** Sistem canlıya çıkmadan önceki
tarih için sinyal üretilmiyor, çünkü o dönemin cüzdan skorları ve
point-in-time snapshot'ları yok. Strategy Lab ilk günden itibaren birikir.

---

## 8. Yedekleme

Sinyaller ve sonuçlar zamanla birikiyor ve yeniden üretilemiyorlar (point-in-time
snapshot'lar geriye dönük hesaplanamaz). Railway Postgres eklentisinde düzenli
yedek al:

```bash
railway run pg_dump "$DATABASE_URL" > yedek-$(date +%F).sql
```

En kritik tablolar: `signals`, `signal_outcomes`, `wallet_trades`.
