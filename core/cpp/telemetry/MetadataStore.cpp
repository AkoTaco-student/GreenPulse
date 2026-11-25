#include "MetadataStore.hpp"
#include <QDateTime>
#include <QDebug>
#include <QMutexLocker>
#include <QTime>
#include <cmath>

MetadataStore::MetadataStore(QObject* parent) : QObject(parent)
{
    connect(&m_tickTimer, &QTimer::timeout, this, &MetadataStore::onTick);
    m_tickTimer.setInterval(1000);
    m_tickTimer.start();
}

double MetadataStore::frequencyHz() const { QMutexLocker lock(&m_mutex); return m_data.frequencyHz; }
double MetadataStore::priceNO1() const { QMutexLocker lock(&m_mutex); return m_data.priceNO1; }
double MetadataStore::priceNO2() const { QMutexLocker lock(&m_mutex); return m_data.priceNO2; }
double MetadataStore::priceNO3() const { QMutexLocker lock(&m_mutex); return m_data.priceNO3; }
double MetadataStore::priceNO4() const { QMutexLocker lock(&m_mutex); return m_data.priceNO4; }
double MetadataStore::priceNO5() const { QMutexLocker lock(&m_mutex); return m_data.priceNO5; }
double MetadataStore::systemPrice() const { QMutexLocker lock(&m_mutex); return m_data.systemPrice; }
bool MetadataStore::hasObservedPrices() const { QMutexLocker lock(&m_mutex); return m_data.hasObservedPrices; }
bool MetadataStore::hasObservedPriceNO1() const { QMutexLocker lock(&m_mutex); return m_data.hasObservedPriceNO1; }
bool MetadataStore::hasObservedPriceNO2() const { QMutexLocker lock(&m_mutex); return m_data.hasObservedPriceNO2; }
bool MetadataStore::hasObservedPriceNO3() const { QMutexLocker lock(&m_mutex); return m_data.hasObservedPriceNO3; }
bool MetadataStore::hasObservedPriceNO4() const { QMutexLocker lock(&m_mutex); return m_data.hasObservedPriceNO4; }
bool MetadataStore::hasObservedPriceNO5() const { QMutexLocker lock(&m_mutex); return m_data.hasObservedPriceNO5; }
QVariantList MetadataStore::observedPriceSeries() const { QMutexLocker lock(&m_mutex); return m_data.observedPriceSeries; }
QString MetadataStore::observedPriceDayStartUtc() const { QMutexLocker lock(&m_mutex); return m_data.observedPriceDayStartUtc; }
QString MetadataStore::observedPriceDayEndUtc() const { QMutexLocker lock(&m_mutex); return m_data.observedPriceDayEndUtc; }
bool MetadataStore::hasModelledPrices() const { QMutexLocker lock(&m_mutex); return m_data.hasModelledPrices; }
QString MetadataStore::modelledDeliveryTimeUtc() const { QMutexLocker lock(&m_mutex); return m_data.modelledDeliveryTimeUtc; }
QString MetadataStore::modelledDeliveryDayLocal() const { QMutexLocker lock(&m_mutex); return m_data.modelledDeliveryDayLocal; }
QString MetadataStore::modelledForecastOriginUtc() const { QMutexLocker lock(&m_mutex); return m_data.modelledForecastOriginUtc; }
QVariantList MetadataStore::modelledPriceSeriesA() const { QMutexLocker lock(&m_mutex); return m_data.modelledPriceSeriesA; }
QVariantList MetadataStore::modelledPriceSeriesB() const { QMutexLocker lock(&m_mutex); return m_data.modelledPriceSeriesB; }
bool MetadataStore::hasPhysicalFlows() const { QMutexLocker lock(&m_mutex); return m_data.hasPhysicalFlows; }
bool MetadataStore::hasFrequency() const { QMutexLocker lock(&m_mutex); return m_data.hasFrequency; }
int MetadataStore::physicalFlowCount() const { QMutexLocker lock(&m_mutex); return m_data.physicalFlowCount; }
QVariantList MetadataStore::physicalFlows() const { QMutexLocker lock(&m_mutex); return m_data.physicalFlows; }

double MetadataStore::obsPriceNO1() const { QMutexLocker lock(&m_mutex); return m_data.obsPriceNO1; }
double MetadataStore::obsPriceNO2() const { QMutexLocker lock(&m_mutex); return m_data.obsPriceNO2; }
double MetadataStore::obsPriceNO3() const { QMutexLocker lock(&m_mutex); return m_data.obsPriceNO3; }
double MetadataStore::obsPriceNO4() const { QMutexLocker lock(&m_mutex); return m_data.obsPriceNO4; }
double MetadataStore::obsPriceNO5() const { QMutexLocker lock(&m_mutex); return m_data.obsPriceNO5; }
double MetadataStore::modPriceNO1() const { QMutexLocker lock(&m_mutex); return m_data.modPriceNO1; }
double MetadataStore::modPriceNO2() const { QMutexLocker lock(&m_mutex); return m_data.modPriceNO2; }
double MetadataStore::modPriceNO3() const { QMutexLocker lock(&m_mutex); return m_data.modPriceNO3; }
double MetadataStore::modPriceNO4() const { QMutexLocker lock(&m_mutex); return m_data.modPriceNO4; }
double MetadataStore::modPriceNO5() const { QMutexLocker lock(&m_mutex); return m_data.modPriceNO5; }

double MetadataStore::flowNO1_NO2() const { QMutexLocker lock(&m_mutex); return m_data.flowNO1_NO2; }
double MetadataStore::flowNO1_NO3() const { QMutexLocker lock(&m_mutex); return m_data.flowNO1_NO3; }
double MetadataStore::flowNO1_NO5() const { QMutexLocker lock(&m_mutex); return m_data.flowNO1_NO5; }
double MetadataStore::flowNO2_NO5() const { QMutexLocker lock(&m_mutex); return m_data.flowNO2_NO5; }
double MetadataStore::flowNO3_NO4() const { QMutexLocker lock(&m_mutex); return m_data.flowNO3_NO4; }
double MetadataStore::flowNO3_NO5() const { QMutexLocker lock(&m_mutex); return m_data.flowNO3_NO5; }
double MetadataStore::netExportEuropeMW() const { QMutexLocker lock(&m_mutex); return m_data.netExportEuropeMW; }
double MetadataStore::mfrrActivatedMW() const { QMutexLocker lock(&m_mutex); return m_data.mfrrActivatedMW; }
double MetadataStore::shadowPriceNO1_NO3() const { QMutexLocker lock(&m_mutex); return m_data.shadowPriceNO1_NO3; }
double MetadataStore::shadowPriceNO3_NO4() const { QMutexLocker lock(&m_mutex); return m_data.shadowPriceNO3_NO4; }

QString MetadataStore::dataSourceMode() const { QMutexLocker lock(&m_mutex); return m_data.dataSourceMode; }
QString MetadataStore::activeScenario() const { QMutexLocker lock(&m_mutex); return m_data.activeScenario; }
QString MetadataStore::dayAheadMode() const { QMutexLocker lock(&m_mutex); return m_data.dayAheadMode; }
QString MetadataStore::sourceStatus() const { QMutexLocker lock(&m_mutex); return m_data.sourceStatus; }
QString MetadataStore::lastUpdatedUtc() const { QMutexLocker lock(&m_mutex); return m_data.lastUpdatedUtc; }
QString MetadataStore::bindingConstraint() const { QMutexLocker lock(&m_mutex); return m_data.bindingConstraint; }
QString MetadataStore::modelBasisExplanation() const { QMutexLocker lock(&m_mutex); return m_data.modelBasisExplanation; }
quint64 MetadataStore::timestampUs() const { QMutexLocker lock(&m_mutex); return m_data.timestampUs; }

double MetadataStore::currentHourFraction() const
{
    const QTime now = QTime::currentTime();
    return now.hour() + now.minute() / 60.0 + now.second() / 3600.0;
}

GridTelemetry MetadataStore::currentTelemetry() const
{
    QMutexLocker lock(&m_mutex);
    return m_data;
}

bool MetadataStore::lineIsBinding(const QString& lineId) const
{
    QMutexLocker lock(&m_mutex);
    if (!m_data.hasModelledPrices)
        return false;
    double flow = 0.0;
    double capacity = 0.0;
    if (lineId == QStringLiteral("L_NO1_NO2")) { flow = m_data.flowNO1_NO2; capacity = m_data.capNO1_NO2; }
    else if (lineId == QStringLiteral("L_NO1_NO3")) { flow = m_data.flowNO1_NO3; capacity = m_data.capNO1_NO3; }
    else if (lineId == QStringLiteral("L_NO1_NO5")) { flow = m_data.flowNO1_NO5; capacity = m_data.capNO1_NO5; }
    else if (lineId == QStringLiteral("L_NO2_NO5")) { flow = m_data.flowNO2_NO5; capacity = m_data.capNO2_NO5; }
    else if (lineId == QStringLiteral("L_NO3_NO4")) { flow = m_data.flowNO3_NO4; capacity = m_data.capNO3_NO4; }
    else if (lineId == QStringLiteral("L_NO3_NO5")) { flow = m_data.flowNO3_NO5; capacity = m_data.capNO3_NO5; }
    return capacity > 0.0 && std::abs(flow) >= capacity * 0.999;
}

double MetadataStore::lineCapacity(const QString& lineId) const
{
    QMutexLocker lock(&m_mutex);
    if (lineId == QStringLiteral("L_NO1_NO2")) return m_data.capNO1_NO2;
    if (lineId == QStringLiteral("L_NO1_NO3")) return m_data.capNO1_NO3;
    if (lineId == QStringLiteral("L_NO1_NO5")) return m_data.capNO1_NO5;
    if (lineId == QStringLiteral("L_NO2_NO5")) return m_data.capNO2_NO5;
    if (lineId == QStringLiteral("L_NO3_NO4")) return m_data.capNO3_NO4;
    if (lineId == QStringLiteral("L_NO3_NO5")) return m_data.capNO3_NO5;
    return 0.0;
}

double MetadataStore::lineShadowPrice(const QString& lineId) const
{
    QMutexLocker lock(&m_mutex);
    if (lineId == QStringLiteral("L_NO1_NO3")) return m_data.shadowPriceNO1_NO3;
    if (lineId == QStringLiteral("L_NO3_NO4")) return m_data.shadowPriceNO3_NO4;
    return 0.0;
}

void MetadataStore::selectDisplayedPrices()
{
    m_data.priceNO1 = m_data.priceNO2 = m_data.priceNO3 = m_data.priceNO4 = m_data.priceNO5 = 0.0;
    m_data.systemPrice = 0.0;
    if (m_data.dataSourceMode == QStringLiteral("MODELLED") && m_data.hasModelledPrices) {
        m_data.priceNO1 = m_data.modPriceNO1;
        m_data.priceNO2 = m_data.modPriceNO2;
        m_data.priceNO3 = m_data.modPriceNO3;
        m_data.priceNO4 = m_data.modPriceNO4;
        m_data.priceNO5 = m_data.modPriceNO5;
    } else if (m_data.dataSourceMode == QStringLiteral("OBSERVED") && m_data.hasObservedPrices) {
        m_data.priceNO1 = m_data.obsPriceNO1;
        m_data.priceNO2 = m_data.obsPriceNO2;
        m_data.priceNO3 = m_data.obsPriceNO3;
        m_data.priceNO4 = m_data.obsPriceNO4;
        m_data.priceNO5 = m_data.obsPriceNO5;
    }
    if ((m_data.dataSourceMode == QStringLiteral("OBSERVED") && m_data.hasObservedPrices)
        || (m_data.dataSourceMode == QStringLiteral("MODELLED") && m_data.hasModelledPrices)) {
        if (m_data.dataSourceMode == QStringLiteral("OBSERVED")) {
            double total = 0.0;
            int count = 0;
            const auto add = [&total, &count](bool available, double price) {
                if (available) { total += price; ++count; }
            };
            add(m_data.hasObservedPriceNO1, m_data.obsPriceNO1);
            add(m_data.hasObservedPriceNO2, m_data.obsPriceNO2);
            add(m_data.hasObservedPriceNO3, m_data.obsPriceNO3);
            add(m_data.hasObservedPriceNO4, m_data.obsPriceNO4);
            add(m_data.hasObservedPriceNO5, m_data.obsPriceNO5);
            m_data.systemPrice = count > 0 ? total / count : 0.0;
        } else {
            m_data.systemPrice = (m_data.priceNO1 + m_data.priceNO2 + m_data.priceNO3
                + m_data.priceNO4 + m_data.priceNO5) / 5.0;
        }
    }
}

void MetadataStore::update(const GridTelemetry& telemetry)
{
    qDebug() << "[telemetry:update] observed=" << telemetry.hasObservedPrices
             << "modeled=" << telemetry.hasModelledPrices
             << "physical_flows=" << telemetry.hasPhysicalFlows
             << "flow_records=" << telemetry.physicalFlowCount
             << "frequency_update=" << telemetry.frequencyUpdated
             << "frequency_valid=" << telemetry.hasFrequency
             << "frequency_hz=" << telemetry.frequencyHz
             << "status=" << telemetry.sourceStatus;
    if (telemetry.hasObservedPrices) {
        qDebug() << "[telemetry:observed-prices EUR/MWh]"
                 << "NO1=" << telemetry.obsPriceNO1 << "NO2=" << telemetry.obsPriceNO2
                 << "NO3=" << telemetry.obsPriceNO3 << "NO4=" << telemetry.obsPriceNO4
                 << "NO5=" << telemetry.obsPriceNO5;
    }
    {
        QMutexLocker lock(&m_mutex);
        if (telemetry.frequencyUpdated) {
            m_data.frequencyHz = telemetry.frequencyHz;
            m_data.hasFrequency = telemetry.hasFrequency;
        }
        if (telemetry.hasObservedPrices) {
            m_data.obsPriceNO1 = telemetry.obsPriceNO1;
            m_data.obsPriceNO2 = telemetry.obsPriceNO2;
            m_data.obsPriceNO3 = telemetry.obsPriceNO3;
            m_data.obsPriceNO4 = telemetry.obsPriceNO4;
            m_data.obsPriceNO5 = telemetry.obsPriceNO5;
            m_data.hasObservedPriceNO1 = telemetry.hasObservedPriceNO1;
            m_data.hasObservedPriceNO2 = telemetry.hasObservedPriceNO2;
            m_data.hasObservedPriceNO3 = telemetry.hasObservedPriceNO3;
            m_data.hasObservedPriceNO4 = telemetry.hasObservedPriceNO4;
            m_data.hasObservedPriceNO5 = telemetry.hasObservedPriceNO5;
            m_data.hasObservedPrices = telemetry.hasObservedPriceNO1 || telemetry.hasObservedPriceNO2
                || telemetry.hasObservedPriceNO3 || telemetry.hasObservedPriceNO4 || telemetry.hasObservedPriceNO5;
            m_data.observedPriceSeries = telemetry.observedPriceSeries;
            m_data.observedPriceDayStartUtc = telemetry.observedPriceDayStartUtc;
            m_data.observedPriceDayEndUtc = telemetry.observedPriceDayEndUtc;
        }
        if (telemetry.hasModelledPrices) {
            m_data.modPriceNO1 = telemetry.modPriceNO1;
            m_data.modPriceNO2 = telemetry.modPriceNO2;
            m_data.modPriceNO3 = telemetry.modPriceNO3;
            m_data.modPriceNO4 = telemetry.modPriceNO4;
            m_data.modPriceNO5 = telemetry.modPriceNO5;
            m_data.flowNO1_NO2 = telemetry.flowNO1_NO2;
            m_data.flowNO1_NO3 = telemetry.flowNO1_NO3;
            m_data.flowNO1_NO5 = telemetry.flowNO1_NO5;
            m_data.flowNO2_NO5 = telemetry.flowNO2_NO5;
            m_data.flowNO3_NO4 = telemetry.flowNO3_NO4;
            m_data.flowNO3_NO5 = telemetry.flowNO3_NO5;
            m_data.capNO1_NO2 = telemetry.capNO1_NO2;
            m_data.capNO1_NO3 = telemetry.capNO1_NO3;
            m_data.capNO1_NO5 = telemetry.capNO1_NO5;
            m_data.capNO2_NO5 = telemetry.capNO2_NO5;
            m_data.capNO3_NO4 = telemetry.capNO3_NO4;
            m_data.capNO3_NO5 = telemetry.capNO3_NO5;
            m_data.shadowPriceNO1_NO3 = telemetry.shadowPriceNO1_NO3;
            m_data.shadowPriceNO3_NO4 = telemetry.shadowPriceNO3_NO4;
            m_data.hasModelledPrices = true;
            m_data.modelledDeliveryTimeUtc = telemetry.modelledDeliveryTimeUtc;
            m_data.modelledDeliveryDayLocal = telemetry.modelledDeliveryDayLocal;
            m_data.modelledForecastOriginUtc = telemetry.modelledForecastOriginUtc;
            m_data.modelledPriceSeriesA = telemetry.modelledPriceSeriesA;
            m_data.modelledPriceSeriesB = telemetry.modelledPriceSeriesB;
            m_data.bindingConstraint = telemetry.bindingConstraint;
            m_data.modelBasisExplanation = telemetry.modelBasisExplanation;
            m_data.activeScenario = telemetry.activeScenario;
        }
        if (telemetry.hasPhysicalFlows) {
            m_data.physicalFlows = telemetry.physicalFlows;
            m_data.physicalFlowCount = telemetry.physicalFlowCount;
            m_data.hasPhysicalFlows = true;
        }
        if (telemetry.hasNetExportEurope) {
            m_data.netExportEuropeMW = telemetry.netExportEuropeMW;
            m_data.hasNetExportEurope = true;
        }
        if (!telemetry.sourceStatus.isEmpty())
            m_data.sourceStatus = telemetry.sourceStatus;
        if (!telemetry.lastUpdatedUtc.isEmpty())
            m_data.lastUpdatedUtc = telemetry.lastUpdatedUtc;
        if (telemetry.timestampUs)
            m_data.timestampUs = telemetry.timestampUs;
        m_data.qualityStatus = telemetry.qualityStatus;
        selectDisplayedPrices();
    }
    emit changed();
}

void MetadataStore::setScenario(const QString& scenarioId)
{
    if (scenarioId.isEmpty())
        return;
    {
        QMutexLocker lock(&m_mutex);
        m_data.activeScenario = scenarioId;
        m_data.sourceStatus = QStringLiteral("Beregner scenario %1…").arg(scenarioId);
    }
    emit changed();
    emit scenarioRequested(scenarioId);
}

void MetadataStore::setDayAheadMode(const QString& requestedMode)
{
    const QString mode = requestedMode.trimmed().toUpper();
    if (mode != QStringLiteral("REPLAY") && mode != QStringLiteral("FORECAST"))
        return;

    QString scenario;
    {
        QMutexLocker lock(&m_mutex);
        if (m_data.dayAheadMode == mode)
            return;
        m_data.dayAheadMode = mode;
        scenario = m_data.activeScenario;
        // Do not leave the previous mode's curve on screen while the new job runs.
        m_data.hasModelledPrices = false;
        m_data.modelledPriceSeriesA.clear();
        m_data.modelledPriceSeriesB.clear();
        m_data.sourceStatus = QStringLiteral("Starting %1 calculation…").arg(mode);
        selectDisplayedPrices();
    }
    emit changed();
    emit dayAheadModeRequested(mode, scenario);
}

void MetadataStore::setDataSourceMode(const QString& mode)
{
    if (mode != QStringLiteral("OBSERVED") && mode != QStringLiteral("MODELLED"))
        return;
    {
        QMutexLocker lock(&m_mutex);
        m_data.dataSourceMode = mode;
        selectDisplayedPrices();
    }
    emit changed();
}

void MetadataStore::toggleDataSourceMode()
{
    setDataSourceMode(dataSourceMode() == QStringLiteral("OBSERVED")
        ? QStringLiteral("MODELLED") : QStringLiteral("OBSERVED"));
}

void MetadataStore::setSourceStatus(const QString& status)
{
    {
        QMutexLocker lock(&m_mutex);
        m_data.sourceStatus = status;
    }
    emit changed();
}

void MetadataStore::reset()
{
    {
        QMutexLocker lock(&m_mutex);
        m_data = GridTelemetry();
        m_data.sourceStatus = QStringLiteral("Initialisert - venter på telemetri");
        m_data.bindingConstraint = QStringLiteral("Ingen beregnet nettstatus ennå.");
        m_data.modelBasisExplanation = QStringLiteral("Venter på modellresultat og observerte priser.");
        selectDisplayedPrices();
    }
    emit changed();
}

void MetadataStore::onTick()
{
    // This timer only refreshes the UI's clock fraction. It never simulates telemetry.
    emit changed();
}
