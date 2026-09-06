# coin-finder — Smart Money Signal & Strategy Lab

## 0. Görev Anlayışım (Understanding)

Referans alınan sistem (AlphaHedgeBot) üç parçadan oluşuyor:

1. **On-chain sinyal motoru** — Kanıtlanmış kârlı ("smart") cüzdanları izler; N tanesi aynı
   tokeni kısa bir pencerede alınca Telegram'a alert atar.
2. **Strategy Lab** — Geçmiş sinyalleri saklayıp filtre kombinasyonlarıyla backtest ettiren
   web paneli.
3. **Para kazanma katmanı** — Telegram aboneliği + opsiyonel otomatik alım (copy trade).

Biz bunun **gerçekten değer üreten** kısmını alıyoruz, pazarlama kısmını değil.

### Referans sistemin dürüst bir okuması (neyi almıyoruz, neden)

Tweet'teki panelde iki sayı yan yana duruyor ve bu ikisi aslında sistemin kendi itirafı:

| Metrik | Tweet'teki değer | Ne demek |
|---|---|---|
| "%50 peak'te çık" | +$251.8k, ROI +%82.5 | **Geriye dönük bakış (look-ahead).** Peak'i ancak sonradan bilirsin. |
| "Reality check: hiç satmamış" | medyan **0.26×**, net +$1.28M ama %81 token flat/rug | Aynı sinyaller, gerçekçi çıkış = sermayenin çoğu yanıyor |
| "Kazanç oranı" %39.9 | 1216 kazanç / 1835 kayıp | Kayıp sayısı kazançtan fazla; her şey kuyruk dağılımına bağlı |

Ayrıca "medyan çarpan 1.54×" **gaz + slippage + swap ücreti + vergi öncesi**. 40k likiditeli bir
tokende $100'lük giriş-çıkışın round-trip maliyeti gerçekte ~%3-8. Bu 1.54× medyanı pratikte
1.4×'e indirir, ve kaybeden trade'lerde kaybı büyütür.

**Kararımız:** Backtest motoru geriye dönük ("peak'te çık") modu **gösterecek ama varsayılan
yapmayacak**. Varsayılan; maliyetli, look-ahead içermeyen, o an alınabilir kararlarla
kurulmuş çıkış modelleri olacak. Bir ürünün asıl değeri sinyalde değil, **sinyalin gerçekte ne
kazandırdığını dürüstçe ölçmesinde.**

### v1 kapsamı (ne inşa ediyoruz)

- Smart-money cüzdan keşfi + sürekli yeniden skorlama (realized PnL, tutarlılık, sybil temizliği)
- Konfluans sinyal motoru (N cüzdan / W pencere) + güvenlik ön-filtresi (honeypot, tax, LP)
- Telegram bot: kişiselleştirilebilir filtreler, alert, /stats, cooldown & spam kontrolü
- Strategy Lab: point-in-time snapshot'lı, maliyet modelli, dürüst backtest + web panel
- Paper trading (forward test): canlı sinyallerin gerçek zamanlı, hilesiz performans karnesi
- Abonelik: HD-türetilmiş kullanıcıya özel deposit adresi (tweet'teki "tag" hilesinden daha temiz)

---

## 1. Mimari Karar Özeti

| Katman | Seçim | Gerekçe |
|---|---|---|
| Dil | **Python 3.11** (asyncio) | Backtest için Polars/NumPy; aiogram olgun; tek dil = tek deploy |
| API | FastAPI + uvicorn | Railway'de tek servis, static SPA'yı da servis eder |
| DB | **PostgreSQL** (Railway addon) | Zaman serisi + ilişkisel; TimescaleDB gerekmez, partition yeter |
| Cache/Queue | **Redis** (Railway addon) | Rate-limit, dedupe, job kuyruğu, pub/sub |
| Backtest | **Polars** | 90 gün × ~50k sinyal × yüzlerce filtre kombosu — pandas'tan ~5-10× hızlı |
| Bot | aiogram 3 | Async, FSM, webhook modu Railway'e uygun |
| Frontend | Vite + React + Recharts | Strategy Lab paneli; build çıktısı FastAPI'den static servis |
| Deploy | Railway (3 servis: api, worker, bot) + Postgres + Redis | Kullanıcının hesabı mevcut |

### Servis topolojisi

```
                    ┌──────────────┐
   RPC / Helius ───▶│  ingest      │  swap event → raw_swaps
   DexScreener  ───▶│  (worker)    │  token meta → tokens, pools
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  scoring     │  realized PnL → wallet_scores
                    │  (cron)      │  sybil cluster → wallet_clusters
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐
                    │  signal      │  konfluans + güvenlik → signals
                    │  engine      │  (immutable point-in-time snapshot)
                    └───┬──────┬───┘
                        │      │
              ┌─────────▼─┐  ┌─▼──────────────┐
              │ telegram  │  │ api + Strategy │
              │   bot     │  │ Lab (FastAPI)  │
              └───────────┘  └────────────────┘
```

---

## 2. Yapılacaklar (Checklist)

### Faz 0 — İskelet & altyapı
- [x] `uv` tabanlı Python paket yapısı (`src/coinfinder/...`), pyproject, ruff + mypy
- [x] Postgres şeması + Alembic migration'ları
- [x] Config katmanı (pydantic-settings, tüm sırlar env'den)
- [x] Docker + `railway.toml` (api / worker / bot servisleri)
- [x] GitHub Actions: ruff, mypy, pytest
- [x] `docker-compose.yml` ile yerel Postgres+Redis

### Faz 1 — Veri katmanı
- [x] DexScreener client (rate-limit'li, retry'lı, circuit breaker)
- [x] GeckoTerminal client (DexScreener fallback + OHLCV backfill)
- [x] Token/pool metadata cache (Redis + Postgres)
- [x] Fiyat snapshot yazıcı (sinyal anındaki durumu dondurmak için — **look-ahead önleme**)

### Faz 2 — On-chain ingestion
- [x] EVM log indexer: Uniswap V2/V3/V4 + Aerodrome + PancakeSwap `Swap` event'leri
- [x] Reorg-safe checkpoint (N blok confirmation + rollback)
- [x] Swap → (wallet, token, yön, miktar, USD) normalizasyonu
- [x] Router/aggregator/MEV bot adres filtresi (1inch, 0x, Banana, Maestro, jaredfromsubway...)
- [ ] Solana ingestion (Helius webhook/gRPC) — *chain kararına bağlı*

### Faz 3 — Smart wallet keşfi & skorlama
- [x] FIFO realized-PnL hesaplayıcı (kısmi çıkışlar, çoklu giriş)
- [x] Cüzdan metrikleri: win-rate, medyan çarpan, PnL, işlem sayısı, aktif gün, avg hold
- [x] Bot/sniper eleme (aynı blokta giriş, >X tx/gün, aynı-nonce paterni)
- [x] **Sybil/cluster tespiti**: ortak fonlama kaynağı → tek varlık say (sahte konfluansı öldürür)
- [x] Zaman ağırlıklı skor (yarı ömür ~30 gün) + minimum örneklem eşiği
- [x] Gün sonu yeniden skorlama cron'u

### Faz 4 — Sinyal motoru
- [x] Konfluans dedektörü: ≥K farklı *cluster* W penceresinde alım
- [x] Güvenlik ön-filtresi: honeypot simülasyonu (`eth_call`), buy/sell tax, LP kilit/burn,
      top-holder yoğunlaşması, deployer geçmişi (daha önce rug çekmiş mi)
- [x] Kalite skoru: **kalibre edilmiş lojistik model** → P(≥2×) — yıldız değil, olasılık
- [x] Dedupe + cooldown (aynı token için tekrar alert kuralları)
- [x] Sinyal snapshot'ı immutable yaz (mcap, likidite, yaş, fiyat, holder, tax — o anki hâli)

### Faz 5 — Telegram bot
- [x] Webhook modu + aiogram router yapısı
- [x] Alert şablonu (mcap/liq/yaş/tax/kalite/CA + Trojan/Maestro/GMGN deep-link'leri)
- [x] Kullanıcı filtre profilleri (chain, min cüzdan, mcap aralığı, min likidite, max yaş)
- [x] `/stats` — kullanıcının kendi filtresinin son 30 gün gerçek performansı
- [x] Rate limit + mesaj gruplama (spam koruması)

### Faz 6 — Strategy Lab (asıl fark yaratan kısım)
- [x] Backtest motoru (Polars): filtre kombosu × çıkış modeli → PnL dağılımı
- [x] **Maliyet modeli**: gaz + DEX fee + likidite derinliğine göre slippage + buy/sell tax
- [x] Çıkış modelleri: sabit TP merdiveni, trailing stop, süre bazlı, hold-to-now (reality check),
      ve *ayrı işaretlenmiş* peak-based (look-ahead uyarısıyla)
- [x] Survivorship raporu: delist/rug olan tokenleri **0 olarak** dahil et
- [x] Bootstrap güven aralığı (medyan ve win-rate için) — tek sayı yerine aralık
- [x] Overfit uyarısı: en iyi 10 komboyu out-of-sample dilimde de göster
- [x] FastAPI endpoint'leri + Vite/React panel (mobil öncelikli, screenshot'lardaki gibi)

### Faz 7 — Paper trading / forward test
- [ ] Her sinyal için sanal pozisyon aç, sabit kurallarla kapat, günlük mark-to-market
- [ ] Herkese açık, değiştirilemez performans sayfası (backtest'e karşı gerçeklik kontrolü)

### Faz 8 — Abonelik & ödeme
- [ ] HD wallet (BIP32) ile kullanıcıya özel deposit adresi türetme
- [ ] Deposit watcher → onay sayısı → abonelik aktivasyonu
- [ ] Plan/limit yönetimi, deneme süresi, süre bitiş uyarısı
- [ ] (Tweet'teki "tutar-tag" yöntemi sadece fallback olarak; adres-başına-kullanıcı daha temiz)

### Faz 9 — Otomatik alım (opsiyonel, karara bağlı)
- [ ] Non-custodial öncelikli tasarım; custodial ise KMS/envelope encryption + withdraw allowlist
- [ ] Pozisyon limiti, günlük kayıp limiti, killswitch
- [ ] Simülasyon-önce-gönder (`eth_call`), slippage koruması, private mempool (MEV koruması)

### Faz 10 — Operasyon
- [x] Structured logging + Sentry
- [ ] Sağlık metrikleri: ingestion gecikmesi, sinyal→alert latency, RPC hata oranı
- [ ] Alarm: veri akışı durursa Telegram'dan admin'e bildirim

---

## 3. Riskler & Dürüst Uyarılar

| Risk | Etki | Azaltma |
|---|---|---|
| **Look-ahead bias** | Backtest gerçekte olmayan kâr gösterir | Point-in-time snapshot, peak modu varsayılan değil |
| **Survivorship bias** | Rug olan tokenler veriden düşer, sonuç şişer | Delist tokenler PnL'e −%100 olarak girer |
| **Overfitting** | "En iyi 10 strateji" geçmişe uydurulmuş olur | Out-of-sample dilim + bootstrap CI zorunlu |
| **Slippage/gaz ihmali** | Lowcap'te tek başına %5-10 fark | Likidite derinliğinden slippage modeli |
| **Sybil konfluans** | Tek kişinin 5 cüzdanı "5 smart wallet" görünür | Fonlama-kaynağı kümelemesi |
| **RPC maliyeti/limiti** | Ingestion durur, sinyal gecikir | Çoklu sağlayıcı + failover + backpressure |
| **Custody riski** | Kullanıcı fonu çalınırsa geri dönüşü yok | v1'de custody yok; olursa KMS + allowlist + limit |
| **Finansal risk (kullanıcı)** | Lowcap memecoin ticareti çoğunlukla zarardır | Panelde gerçek dağılımı göster, "sinyal ≠ tavsiye" |

> **Not:** Bu bir ticaret tavsiyesi sistemi değil, bir veri/analiz aracı. Kullanıcı arayüzünün
> her yerinde gerçek kayıp oranı (referans sistemde bile %60 kayıp) açıkça görünmeli.

---

## 4. Review / Sonuçlar

### Teslim edilen

Çalışan, deploy edilebilir bir sistem: **41 kaynak dosya, 171 test, ruff + mypy temiz.**

| Katman | Durum |
|---|---|
| Chain registry (Base/Robinhood/BSC) | ✅ saf veri; zincir eklemek ingestion koduna dokunmuyor |
| RPC havuzu | ✅ rotasyon, token-bucket, 429 cooldown, adaptif blok aralığı |
| Cüzdan-merkezli indeksleme | ✅ ücretsiz katmanı mümkün kılan topic-filter yaklaşımı |
| İşlem çıkarımı | ✅ native ETH alımı, router'daki quote leg, LP mint/burn, ERC721 — 15 test |
| FIFO PnL + skorlama | ✅ shrinkage, zaman sönümü, bot eleme — 30 test |
| Sybil kümeleme | ✅ co-buy zamanlamasından; ekstra RPC maliyeti yok |
| Sinyal motoru + güvenlik | ✅ 25 test |
| Backtest (Strategy Lab) | ✅ maliyet modeli, look-ahead etiketleme, OOS bölme, bootstrap CI |
| Telegram bot | ✅ filtreler, alert, /stats |
| API + panel | ✅ headless tarayıcıda doğrulandı, sıfır konsol hatası |
| Deploy (Railway) | ✅ Dockerfile + railway.toml + CI |

### Doğrulama sırasında bulunan ve düzeltilen gerçek hatalar

Bunlar teoride değil, sistemi çalıştırırken çıktı:

1. **`upsert_wallets` çöküyordu** — bir cüzdan tek tarama penceresinde 2+ işlem
   yaptığında (normal durum) `ON CONFLICT DO UPDATE` aynı satıra iki kez dokunuyor
   ve `CardinalityViolation` fırlatıyordu. **Tüm ingestion yazımı düşerdi.**
2. **Sonuç takibi SQL'i reddediliyordu** — Postgres aynı parametre için `> 0`'dan
   integer, bölmeden double çıkarımı yapıp belirsizlik hatası veriyordu.
   **Hiçbir sinyal sonucu kaydedilemezdi.**
3. **Bot-frekans filtresi meşru cüzdanları eliyordu** — gözlem aralığı `.days` ile
   tam sayıya yuvarlanıyordu; tek bir öğleden sonra aktif olan cüzdan "günde 60
   işlem yapan bot" görünüyordu.
4. **Strateji taraması 28.5 saniye sürüyordu** — 576 kombonun hepsi için bootstrap
   hesaplanıyordu. Artık sadece döndürülen sonuçlar için: **4.6 saniye.**
5. **Liderlik tablosu ezberlenmiş stratejileri ödüllendiriyordu** — ham ROI'ye göre
   sıralama ilk 4 sırayı n=30'luk örneklemlere veriyordu ve hepsi out-of-sample'da
   çöküyordu (+%194 → −%29). Artık kanıt miktarına göre sönümlenmiş ROI ile
   sıralanıyor; ilk 10'un tamamı n≥87 ve OOS'ta pozitif.
6. **Dağılım çubukları hiç çizilmiyordu** — track ve fill inline `<span>`'di ve
   inline elemanlarda `width` uygulanmaz. Markup okuyarak değil, headless
   Chromium'da kutu ölçerek bulundu.
7. **Kullanılamaz likidite 0 yerine 0.0001× dönüyordu** — %99 sürtünme tavanı
   sızdırıyordu; her toplamda rug'ları olduğundan iyi gösterirdi.

### Referans sistemle karşılaştırma

Sentetik popülasyon, onların *kendi yayınladıkları* dağılıma kalibre edildi.
Kalibrasyon kontrolü: hold-to-now kazanç oranımız **%18.6**, onların yayınladığı
**%18.8** — popülasyon şekli doğru yeniden üretilmiş.

| Çıkış kuralı | Hindsight? | ROI |
|---|---|---|
| `peak_50pct` (onların manşet modeli) | **evet** | **+%80.8** (onlar: +%82.5) |
| `time_24h` | hayır | +%79.6 |
| `hold_to_now` | hayır | +%67.5 |
| `tp_2x` | hayır | **−%46.0** |

Aynı sinyaller, aynı pozisyon boyutu. Fark tamamen çıkış kuralında.

Pozisyon boyutunun en iyi gerçekçi stratejiye etkisi:

| Boyut | ROI |
|---|---|
| $100 | +%79.6 |
| $500 | +%44.9 |
| $2.000 | **−%1.8** |
| $10.000 | **−%54.9** |

### Yapılmayanlar (bilinçli, kapsam kararı)

- **Faz 8 — abonelik/ödeme:** v1 kapsamı dışı. `users` tablosunda trial/plan
  alanları hazır ama HD deposit adresi türetme yazılmadı.
- **Faz 9 — otomatik alım:** senin kararınla kapsam dışı. Kodun hiçbir yerinde
  private key tutulmuyor; custody yok.
- **Paper trading döngüsü:** `paper_positions` tablosu şemada var, forward-test
  döngüsü yazılmadı.
- **Solana:** EVM dışı olduğu için kapsam dışı bırakıldı.
- **Canlı API doğrulaması:** bu geliştirme ortamının egress politikası
  DexScreener / GeckoTerminal / tüm public RPC'leri 403 ile kapatıyor
  (bkz. `lessons.md`). Parsing fixture'larla test edildi; **canlı uyumluluk
  Railway'de ilk deploy sonrası `/api/stats` ile doğrulanmalı.**

---

## 5. Faz 11 — Kod bilmeyen kullanıcı için uyarlama

**Sorun:** Sistem geliştirici varsayımıyla kuruldu. Kullanıcı kodlama bilmiyor.
Rehber yazmak yetmez; şu üç şey terminal gerektiriyordu:

1. Üç ayrı Railway servisi kurmak (üç farklı başlangıç komutu)
2. `railway run python scripts/smoke_check.py` ile teşhis
3. `curl /api/stats` ile sağlık kontrolü, log okuyarak sorun bulma

**Çözüm:** Terminali tamamen devre dışı bırak. Kullanıcının tek arayüzü
Telegram + web paneli olsun.

- [x] Hepsi-bir-arada çalıştırma modu (`python -m coinfinder`) → Railway'de
      üç servis yerine **tek servis**, tek tıkla deploy
- [x] Ortak teşhis modülü (`diagnostics.py`) — API ve bot aynı kodu kullansın
- [x] `/api/diagnostics` endpoint'i + panelde "Sistem Durumu" kartı
      (sade Türkçe: "✅ Base bağlı", "⏳ Cüzdan aranıyor, ~2 gün")
- [x] Telegram `/durum` komutu — teşhisi sohbette, sade dille göster
- [x] Isınma ilerlemesi göstergesi — "neden hâlâ sinyal yok?" sorusunu cevaplasın
- [x] `docs/BASLANGIC.md` — tıkla-adım-adım, terminal komutu içermeyen rehber
- [x] `docs/KULLANIM.md` — günlük kullanım: alert nasıl okunur, filtreler,
      Strategy Lab nasıl yorumlanır

### Faz 11 sonucu

Terminal gerekliliği tamamen kaldırıldı. Kullanıcının tek arayüzü Telegram + panel.

**Yapılan:**
- Tek servis (`python -m coinfinder`) — Railway'de üç yerine bir deployment
- Sade Türkçe teşhis: `/durum` komutu ve panelin en üstündeki sistem kartı
- Panel tamamen Türkçeleştirildi (sayı biçimleri dahil: `%24,9`, `$102,2B`)
- Filtre ve çıkış kuralı etiketleri çevrildi (`4+ cüzdan / MC 0-500b`, `4 saat sonra sat`)
- `docs/BASLANGIC.md` — tıkla-adım-adım kurulum, tek satır komut yok
- `docs/KULLANIM.md` — alert nasıl okunur, filtreler, Strateji Lab yorumu

**Bu fazda bulunan gerçek hatalar:**

8. **Token yokken tüm sistem kapanıyordu.** `run_bot` token bulamayınca dönüyordu,
   süpervizör bunu "bir bileşen öldü" sayıp süreci kapatıyordu. Railway'de bu,
   kullanıcının hiçbir açıklama göremediği sonsuz yeniden başlatma döngüsü demekti —
   tam da önlemeye çalıştığım senaryo. Devre dışı bileşen artık çıkmıyor, bekliyor.
9. **Veritabanı erişilemezse uygulama çöküyordu.** Artık 6 kez geri çekilerek deneniyor,
   sonra API yine de açılıp sorunu sade dille anlatan teşhis sayfasını sunuyor.
10. **`/health` bozuk modda 503 dönüyordu.** Railway bunu görüp servisi sürekli yeniden
    başlatırdı ve kullanıcı teşhis sayfasını hiç göremezdi. Sağlık kontrolü artık
    canlılık bildiriyor; gerçek durum `/api/diagnostics`'te.
11. **İlerleme göstergesi tutarsızdı.** Tamamlanmış adımların *arkasında* kalan boş bir
    adım "X bulununca başlar" diyordu — sonraki adımlar zaten bitmişken bu arıza gibi
    okunuyordu.

---

## 6. Faz 12 — Küçük pozisyon ($5-20) için uyarlama

**Kullanıcının gerçek durumu:** işlem başına $5-20. Bu, maliyet yapısını
tamamen değiştiriyor ve sistemdeki bazı varsayımları geçersiz kılıyor.

### Ölçülen gerçekler (kendi maliyet modelimizden)

Gidiş-dönüş maliyeti, $40k likidite:

| Pozisyon | Base | Robinhood | BNB Chain |
|---|---|---|---|
| $5 | %1,45 | %1,05 | **%5,45** |
| $10 | %1,10 | %0,90 | **%3,10** |
| $20 | %1,00 | %0,90 | **%2,00** |

$10'luk pozisyonda maliyetin dağılımı:
- Base: gaz %0,40 + ücret %0,60 + kayma %0,10 = **%1,10**
- BNB: gaz **%2,40** + ücret %0,60 + kayma %0,10 = **%3,10**

**Sonuç 1:** Bu boyutta baskın maliyet kayma değil **gaz**. BNB Chain gazı
Base'in 6 katı, dolayısıyla bu boyutta 3-5 kat pahalı.

**Sonuç 2:** $5-20 aralığı Base'de aslında **optimuma yakın**. Maliyet eğrisi
$10-20 civarında dipte. Büyük pozisyon daha iyi değil.

**Sonuç 3:** En büyük kaldıraç vergi. $10 pozisyonda %5/%5 vergili token
gidiş-dönüş %10,81 — temiz tokenin 10 katı.

### Yapılacaklar

- [x] Telegram botunun tüm mesajlarını Türkçeleştir
- [x] Kullanıcı başına işlem boyutu ayarı (`trade_size_usd`) — alert'teki
      maliyet satırı $100 yerine **kendi boyutunda** hesaplansın
- [x] Kullanıcı başına maliyet tavanı filtresi — "bana gidiş-dönüş maliyeti
      %X'i geçen sinyal gönderme"
- [x] Alert'te vergi ölçülemediğini açıkça belirt (ücretsiz katmanda
      simülasyon yapılamıyor, en büyük risk kalemi bu)
- [x] Rehberlere küçük-pozisyon bölümü ekle

### Faz 12 sonucu

- Telegram botu tamamen Türkçe (`/ayarlar`, `/durum`, `/karne` takma adlarıyla)
- `trade_size_usd` kullanıcı ayarı: alert'teki maliyet satırı artık sabit $100
  değil, kullanıcının kendi boyutunda hesaplanıyor
- `max_cost_pct` filtresi: kullanıcının boyutunda gidiş-dönüş maliyeti tavanı
  aşan sinyaller hiç gönderilmiyor. Maliyet hesabı SQL'e kopyalanmadı —
  `backtest.costs` tek kaynak olarak kaldı, filtreleme Python tarafında
- Gaz baskınsa alert'te ayrı uyarı satırı ("bunun %2,4 puanı gaz")
- Vergi ölçülemediği her alert'te açıkça yazıyor
- `/ayarlar` ekranı seçilen boyutun üç zincirdeki somut sonucunu gösteriyor

Test: 224 test geçiyor (30 yeni: format, dispatch, kullanıcı boyutlandırma).
