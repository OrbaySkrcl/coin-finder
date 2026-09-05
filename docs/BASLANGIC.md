# Kurulum Rehberi

**Kod yazmanız gerekmiyor.** Terminal, komut satırı, hiçbiri yok. Sadece üç web
sitesinde birkaç düğmeye basacaksınız.

**Süre:** yaklaşık 20 dakika.
**Maliyet:** Railway'in ücretsiz katmanı başlangıç için yeterli. Sistem hiçbir
ücretli veri servisi kullanmıyor.

---

## Neye ihtiyacınız var

| # | Hesap | Ne için | Ücret |
|---|---|---|---|
| 1 | [GitHub](https://github.com) | Kodun durduğu yer | Ücretsiz |
| 2 | [Railway](https://railway.app) | Sistemin çalıştığı yer | Ücretsiz katman |
| 3 | Telegram | Botun kendisi | Ücretsiz |

GitHub hesabınız zaten var — kod orada duruyor.

---

## Adım 1 — Telegram botunuzu oluşturun

Bunu ilk yapıyoruz çünkü sonraki adımda gereken bir şifre üretiyor.

1. Telegram'ı açın, arama kutusuna **`@BotFather`** yazın ve ona mesaj atın.
2. `/newbot` yazıp gönderin.
3. BotFather botunuzun **adını** soracak. İstediğinizi yazın (örn. `AlphaHedge`).
4. Sonra **kullanıcı adını** soracak. `bot` ile bitmek zorunda
   (örn. `alphahedge_signals_bot`). Alınmışsa başka bir şey deneyin.
5. BotFather size şuna benzeyen uzun bir yazı verecek:

   ```
   8123456789:AAF-x9Kd0pQ2mNvR7sT1uW3yZ5aB6cD8eFg
   ```

   **Bu sizin bot şifreniz (token).** Kopyalayın, bir kenara not edin.
   Kimseyle paylaşmayın — bu şifreyi alan botunuzu ele geçirir.

> Ayrıca kendi Telegram numaranızı öğrenin: **`@userinfobot`**'a mesaj atın,
> size bir sayı verecek (örn. `512345678`). Bunu da not edin.

---

## Adım 2 — Railway'de proje açın

1. [railway.app](https://railway.app) → **Login with GitHub** ile girin.
2. **New Project** düğmesine basın.
3. **Deploy from GitHub repo** seçeneğini seçin.
4. Listeden **`coin-finder`** deposunu seçin.
   - Görünmüyorsa: **Configure GitHub App** deyip Railway'e bu depoya erişim
     izni verin, sonra geri dönün.
5. Railway kurulumu başlatacak. Bir-iki dakika sürer.

> **Doğru dal seçili mi?** Railway varsayılan dalı kullanır. Kod
> `claude/100x-token-strategy-7hdfdr` dalında. Servis ayarlarında
> **Settings → Source → Branch** kısmından bu dalı seçin (veya bu dalı
> GitHub'da ana dalla birleştirin).

İlk kurulum **başarısız olacak**. Bu normal — henüz veritabanı yok. Devam edin.

---

## Adım 3 — Veritabanı ekleyin

1. Proje ekranında **`+ New`** düğmesine basın.
2. **Database** → **Add PostgreSQL** seçin.
3. Birkaç saniye içinde `Postgres` adında bir kutu belirecek.

Başka hiçbir şey yapmanıza gerek yok. Tabloları sistem kendisi kuracak.

---

## Adım 4 — İki ayar girin

1. Projenizdeki **coin-finder** kutusuna tıklayın (Postgres'e değil).
2. Üstteki **Variables** sekmesine geçin.
3. **`+ New Variable`** ile şu ikisini tek tek ekleyin:

| Değişken adı | Değer |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `TELEGRAM_BOT_TOKEN` | Adım 1'de aldığınız uzun şifre |

> `DATABASE_URL` değerini **tam olarak yukarıdaki gibi**, süslü parantezlerle
> yazın. Railway bunu otomatik olarak gerçek adrese çevirir.

İsteğe bağlı ama önerilir:

| Değişken adı | Değer | Ne işe yarar |
|---|---|---|
| `TELEGRAM_ADMIN_IDS` | `@userinfobot`'un verdiği sayı | Sizi yönetici olarak tanır |

4. Railway ayarları kaydedince otomatik olarak yeniden kurulum yapar.
   **2-3 dakika bekleyin.**

---

## Adım 5 — Panelin adresini alın

1. Aynı **coin-finder** kutusunda **Settings** sekmesine geçin.
2. **Networking** başlığı altında **Generate Domain** düğmesine basın.
3. Size şuna benzer bir adres verecek:
   `coin-finder-production-1a2b.up.railway.app`

**Bu adresi tarayıcıda açın.** Panel açılıyorsa kurulum tamam.

---

## Adım 6 — Çalıştığını doğrulayın

İki yerden kontrol edebilirsiniz, ikisi de aynı bilgiyi verir:

**Panelden:** Adresi açın. En üstteki **"Sistem durumu"** kartına bakın.

**Telegram'dan:** Botunuza gidin, `/start` yazın, sonra `/durum` yazın.

Göreceğiniz şey şuna benzer:

```
⏳ Isınma sürüyor (aday cüzdanları bulma).
   İlk sinyaller için 2-4 gün normaldir.

✅ Veritabanı — Bağlı ve yazılabilir.
✅ Base bağlantısı — Bağlı, blok 24.881.302.
✅ Robinhood Chain bağlantısı — Bağlı, blok 8.117.940.
✅ BNB Chain bağlantısı — Bağlı, blok 51.203.887.
✅ Fiyat verisi (DexScreener) — Bağlı.
✅ Telegram botu — Token tanımlı, bot açık.

Kurulum ilerlemesi
⏳ 1. Aday cüzdanları bulma — Henüz başlamadı. İlk tarama ~1 saat içinde.
▫️ 2. İşlemlerini izleme — Cüzdan bulunduktan sonra başlar.
▫️ 3. Cüzdanları puanlama — Her cüzdan için en az 8 tamamlanmış alım-satım.
▫️ 4. Sinyal üretme — Aynı tokeni 3 bağımsız akıllı cüzdan alınca tetiklenir.
```

**Yeşil tikler varsa kurulum bitti.** Kırmızı varsa aşağıdaki tabloya bakın.

---

## Ne zaman sinyal gelmeye başlar?

**Hemen değil.** Bu bir hata değil, tasarım.

Sistemin önce kimin kârlı işlem yaptığını öğrenmesi gerekiyor. Bir cüzdanı
"akıllı para" saymak için en az **8 tamamlanmış alım-satım** görmesi lazım.
O kadar geçmiş birikene kadar sessiz kalır.

| Ne zaman | Ne olur |
|---|---|
| İlk 1 saat | Son kazanan tokenlerin erken alıcıları bulunuyor |
| 1-24 saat | Bu cüzdanların işlemleri kaydediliyor |
| 1-3 gün | Cüzdanlar puanlanıyor, en iyileri izleme listesine giriyor |
| **2-4 gün** | **İlk sinyaller** |

Aradaki süre boyunca `/durum` komutu size hangi adımda olduğunuzu söyler.
İlerleme çubuğu ilerliyorsa her şey yolunda demektir.

> **Panel bu sürede boş görünür.** Örnek veriyle doldurma özelliği var ama
> kasıtlı olarak sadece geliştirici tarafında bırakıldı: sahte verinin gerçek
> veriyle karışması, bu tür bir sistemde yapılabilecek en tehlikeli hatadır.
> Görmek isterseniz söyleyin, sizin için bir kez çalıştırılır.

---

## Bir şeyler ters giderse

Önce `/durum` yazın veya panelin üst kartına bakın. Kırmızı satır size ne
olduğunu söyleyecek. Aşağıda en sık görülenler:

| Gördüğünüz | Anlamı | Ne yapmalı |
|---|---|---|
| ❌ **Veritabanı — Bağlanamıyorum** | PostgreSQL eklenmemiş veya `DATABASE_URL` yanlış | Adım 3 ve 4'ü tekrar kontrol edin. Değer tam olarak `${{Postgres.DATABASE_URL}}` olmalı |
| ⚠️ **Telegram botu — Token tanımlı değil** | `TELEGRAM_BOT_TOKEN` girilmemiş | Adım 4'e dönün. Panel yine de çalışır, sadece bot kapalıdır |
| ❌ **Base/BNB bağlantısı — cevap vermedi** | Ücretsiz sunucular geçici olarak dolu | Genelde kendiliğinden düzelir. 3-4 saat sürerse haber verin |
| ❌ **Fiyat verisi — Ulaşamıyorum** | DexScreener'a erişilemiyor | Genelde geçici. Sürerse haber verin |
| Panel hiç açılmıyor | Servis çalışmıyor | Railway'de **Deployments** sekmesine bakın. Kırmızı "Failed" varsa üstüne tıklayıp **Redeploy** deyin |
| 3 gündür aynı adımda | Beklenenden yavaş | `/durum` çıktısının ekran görüntüsünü alıp haber verin |

---

## Aylık maliyet

| Kalem | Ücret |
|---|---|
| Railway uygulama | Ücretsiz katmanda başlar; yoğunlaşırsa ~$5/ay |
| Railway PostgreSQL | Ücretsiz katmanda başlar; ~$5/ay'a çıkabilir |
| Veri servisleri (DexScreener, blockchain) | **$0** — hepsi ücretsiz katman |
| **Toplam** | **$0 – $10/ay** |

Sistem baştan ücretsiz veri kaynaklarıyla çalışacak şekilde tasarlandı. İleride
daha hızlı sinyal isterseniz ücretli bir sağlayıcıya geçmek mümkün, ama
**kod değişikliği gerektirmiyor** — sadece bir ayar eklenir.

---

## Sonraki adım

Kurulum bitti. Şimdi [**Kullanım Rehberi**](KULLANIM.md)'ne geçin: sinyaller
nasıl okunur, filtreler nasıl ayarlanır, Strateji Lab'daki sayılar ne anlama
gelir.
