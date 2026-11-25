#include "StatnettClient.hpp"
#include "StatnettReplyUtils.hpp"

#include <QDate>
#include <QDateTime>
#include <QDebug>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonValue>
#include <QMap>
#include <QList>
#include <QNetworkRequest>
#include <QXmlStreamReader>
#include <QTime>
#include <QTimeZone>
#include <QVariantMap>
#include <cmath>

using StatnettReplyUtils::utcNow;
using StatnettReplyUtils::elementText;
using StatnettReplyUtils::parseNumber;

namespace {
QString areaFromJson(const QJsonObject& object)
{
    return elementText(object, {"Area", "area", "AreaId", "AreaCode", "ElspotArea", "InAreaElspotId"})
        .trimmed().toUpper();
}

void collectJsonPrices(const QJsonValue& value, QMap<QString, QList<double>>& prices)
{
    if (value.isArray()) {
        for (const QJsonValue& item : value.toArray())
            collectJsonPrices(item, prices);
        return;
    }
    if (!value.isObject())
        return;
    const QJsonObject object = value.toObject();
    const QString area = areaFromJson(object);
    double price = 0.0;
    if (area.startsWith(QStringLiteral("NO"))
        && parseNumber(elementText(object, {"Price", "Value", "price", "Amount", "price_eur_mwh"}), &price)) {
        prices[area].append(price);
    }
    for (auto it = object.constBegin(); it != object.constEnd(); ++it) {
        if (it.value().isArray() || it.value().isObject())
            collectJsonPrices(it.value(), prices);
    }
}

QMap<QString, double> parseXmlPrices(const QByteArray& data, QString* error)
{
    QMap<QString, double> prices;
    QXmlStreamReader xml(data);
    QString area;
    double price = 0.0;
    bool hasPrice = false;
    while (!xml.atEnd()) {
        xml.readNext();
        if (xml.isStartElement()) {
            const QString name = xml.name().toString();
            if (name == QStringLiteral("PriceItem") || name == QStringLiteral("Price")) {
                area.clear();
                hasPrice = false;
            }
            if (name == QStringLiteral("Area") || name == QStringLiteral("AreaId")
                || name == QStringLiteral("AreaCode") || name == QStringLiteral("ElspotArea")) {
                area = xml.readElementText().trimmed().toUpper();
            } else if (name == QStringLiteral("Value") || name == QStringLiteral("PriceAmount")) {
                hasPrice = parseNumber(xml.readElementText().trimmed(), &price);
            } else if (name == QStringLiteral("Price") && !xml.attributes().isEmpty()) {
                area = xml.attributes().value(QStringLiteral("Area")).toString().toUpper();
            }
        } else if (xml.isEndElement()
            && (xml.name() == QStringLiteral("PriceItem") || xml.name() == QStringLiteral("Price"))) {
            if (area.startsWith(QStringLiteral("NO")) && hasPrice)
                prices.insert(area, price);
        }
    }
    if (xml.hasError() && error)
        *error = xml.errorString();
    return prices;
}

void copyPricesToTelemetry(const QMap<QString, double>& prices, GridTelemetry* telemetry)
{
    if (!telemetry)
        return;
    const auto set = [&prices](const QString& area, double* destination, bool* isValid) {
        *isValid = prices.contains(area);
        *destination = *isValid ? prices.value(area) : 0.0;
    };
    set(QStringLiteral("NO1"), &telemetry->obsPriceNO1, &telemetry->hasObservedPriceNO1);
    set(QStringLiteral("NO2"), &telemetry->obsPriceNO2, &telemetry->hasObservedPriceNO2);
    set(QStringLiteral("NO3"), &telemetry->obsPriceNO3, &telemetry->hasObservedPriceNO3);
    set(QStringLiteral("NO4"), &telemetry->obsPriceNO4, &telemetry->hasObservedPriceNO4);
    set(QStringLiteral("NO5"), &telemetry->obsPriceNO5, &telemetry->hasObservedPriceNO5);
    telemetry->hasObservedPrices = telemetry->hasObservedPriceNO1 || telemetry->hasObservedPriceNO2
        || telemetry->hasObservedPriceNO3 || telemetry->hasObservedPriceNO4 || telemetry->hasObservedPriceNO5;
}
}

void StatnettClient::onPricesReply(QNetworkReply* reply)
{
    if (reply->error() != QNetworkReply::NoError) {
        reportFailure(reply, QStringLiteral("ENTSO-E day-ahead prices"));
        return;
    }
    const QByteArray data = reply->readAll();
    /*qDebug().noquote() << "[feed:prices] response http="
                       << reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt()
                       << "bytes=" << data.size();*/
    emit rawDataReceived(QStringLiteral("ENTSO-E A44 prices"), data);
    QMap<QString, double> prices;
    QMap<QString, QString> selectedPriceTimestamps;
    QMap<QString, int> selectedPriceResolutions;
    QVariantList observedPriceSeries;
    QString parseError;
    if (data.trimmed().startsWith('<')) {
        prices = parseXmlPrices(data, &parseError);
    } else {
        const QJsonDocument document = QJsonDocument::fromJson(data);
        if (!document.isNull()) {
            const QJsonObject root = document.object();
            const QJsonArray records = root.value(QStringLiteral("records")).toArray();
            if (!records.isEmpty()) {
                QMap<QString, QDateTime> latestTimes;
                const QDateTime now = QDateTime::currentDateTimeUtc();
                const QTimeZone oslo("Europe/Oslo");
                QDateTime earliestUtc;
                QDateTime latestUtc;
                for (const QJsonValue& value : records) {
                    if (!value.isObject())
                        continue;
                    const QJsonObject record = value.toObject();
                    const QString area = record.value(QStringLiteral("area")).toString().trimmed().toUpper();
                    QDateTime timestamp = QDateTime::fromString(
                        record.value(QStringLiteral("timestamp")).toString(), Qt::ISODateWithMs);
                    if (!timestamp.isValid())
                        timestamp = QDateTime::fromString(record.value(QStringLiteral("timestamp")).toString(), Qt::ISODate);
                    if (!area.startsWith(QStringLiteral("NO")) || !timestamp.isValid())
                        continue;
                    const QDateTime timestampUtc = timestamp.toUTC();
                    if (!earliestUtc.isValid() || timestampUtc < earliestUtc)
                        earliestUtc = timestampUtc;
                    if (!latestUtc.isValid() || timestampUtc > latestUtc)
                        latestUtc = timestampUtc;
                }

                if (!earliestUtc.isValid()) {
                    parseError = QStringLiteral("No valid timestamped Norwegian ENTSO-E records");
                } else {
                    const QDate deliveryDate = earliestUtc.toTimeZone(oslo).date();
                    const QDateTime marketStart(deliveryDate, QTime(0, 0), oslo);
                    const QDateTime marketEnd(deliveryDate.addDays(1), QTime(0, 0), oslo);
                    m_latestTelemetry.observedPriceDayStartUtc = marketStart.toUTC().toString(Qt::ISODateWithMs);
                    m_latestTelemetry.observedPriceDayEndUtc = marketEnd.toUTC().toString(Qt::ISODateWithMs);
                    qDebug().noquote() << "[feed:prices] delivery_day=" << deliveryDate.toString(Qt::ISODate)
                                       << "range=" << m_latestTelemetry.observedPriceDayStartUtc
                                       << "to=" << m_latestTelemetry.observedPriceDayEndUtc;

                    for (const QJsonValue& value : records) {
                        if (!value.isObject())
                            continue;
                        const QJsonObject record = value.toObject();
                        const QString area = record.value(QStringLiteral("area")).toString().trimmed().toUpper();
                        QDateTime timestamp = QDateTime::fromString(
                            record.value(QStringLiteral("timestamp")).toString(), Qt::ISODateWithMs);
                        if (!timestamp.isValid())
                            timestamp = QDateTime::fromString(record.value(QStringLiteral("timestamp")).toString(), Qt::ISODate);
                        double price = 0.0;
                        if (!area.startsWith(QStringLiteral("NO")) || !timestamp.isValid()
                            || !parseNumber(record.value(QStringLiteral("price_eur_mwh")).toVariant().toString(), &price))
                            continue;
                        const QDateTime timestampUtc = timestamp.toUTC();
                        const int resolutionMinutes = record.value(QStringLiteral("resolution_minutes")).toInt(15);
                        if (timestampUtc >= marketStart.toUTC() && timestampUtc < marketEnd.toUTC()) {
                            observedPriceSeries.append(QVariantMap{
                                {QStringLiteral("area"), area},
                                {QStringLiteral("timestamp_utc"), timestampUtc.toString(Qt::ISODateWithMs)},
                                {QStringLiteral("timestamp_ms"), timestampUtc.toMSecsSinceEpoch()},
                                {QStringLiteral("price_eur_mwh"), price},
                                {QStringLiteral("resolution_minutes"), resolutionMinutes},
                            });
                        }
                        if (timestampUtc <= now
                            && (!latestTimes.contains(area) || timestamp > latestTimes.value(area))) {
                            latestTimes.insert(area, timestamp);
                            prices.insert(area, price);
                            selectedPriceTimestamps.insert(area, timestamp.toUTC().toString(Qt::ISODate));
                            selectedPriceResolutions.insert(area, resolutionMinutes);
                        }
                    }

                    static QDate lastRequestedObservedDeliveryDay;
                    static QString lastRequestedObservedScenario;
                    static QString lastRequestedObservedMode;
                    if (lastRequestedObservedDeliveryDay != deliveryDate
                        || lastRequestedObservedScenario != m_currentDayAheadScenario
                        || lastRequestedObservedMode != m_currentDayAheadMode) {
                        lastRequestedObservedDeliveryDay = deliveryDate;
                        lastRequestedObservedScenario = m_currentDayAheadScenario;
                        lastRequestedObservedMode = m_currentDayAheadMode;
                        // Observed A44 refreshes must never force REPLAY when
                        // the QML selector is currently set to FORECAST.
                        fetchModelledDispatchForMode(
                            m_currentDayAheadScenario, m_currentDayAheadMode);
                    }
                }
            } else {
                QMap<QString, QList<double>> series;
                collectJsonPrices(document.isArray() ? QJsonValue(document.array()) : QJsonValue(root), series);
                for (auto it = series.constBegin(); it != series.constEnd(); ++it) {
                    double sum = 0.0;
                    for (double value : it.value()) sum += value;
                    if (!it.value().isEmpty()) prices.insert(it.key(), sum / it.value().size());
                }
            }
        } else {
            parseError = QStringLiteral("Ugyldig JSON-respons");
        }
    }
    if (prices.isEmpty()) {
        qWarning().noquote() << "[feed:prices] parsed zero zone prices; parseError=" << parseError;
        emit fetchError(QStringLiteral("ENTSO-E A44-priser kunne ikke normaliseres: %1").arg(parseError));
    } else {
        /*qDebug() << "[feed:prices] latest parsed EUR/MWh per area:" << prices
             << "timestamps_utc=" << selectedPriceTimestamps
             << "resolution_minutes=" << selectedPriceResolutions
             << "complete=" << (prices.size() == 5);*/
        copyPricesToTelemetry(prices, &m_latestTelemetry);
        m_latestTelemetry.observedPriceSeries = observedPriceSeries;
        m_latestTelemetry.lastUpdatedUtc = utcNow();
        m_latestTelemetry.sourceStatus = prices.size() == 5
            ? QStringLiteral("Observerte priser fra ENTSO-E")
            : QStringLiteral("Delvise observerte priser fra ENTSO-E");
        emit telemetryReceived(m_latestTelemetry);
    }
    reply->deleteLater();
}
