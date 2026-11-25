#pragma once

#include "GridTelemetry.hpp"

#include <QMutex>
#include <QObject>
#include <QString>
#include <QTimer>
#include <QVariantList>

class MetadataStore : public QObject
{
    Q_OBJECT

    Q_PROPERTY(double frequencyHz READ frequencyHz NOTIFY changed)
    Q_PROPERTY(double priceNO1 READ priceNO1 NOTIFY changed)
    Q_PROPERTY(double priceNO2 READ priceNO2 NOTIFY changed)
    Q_PROPERTY(double priceNO3 READ priceNO3 NOTIFY changed)
    Q_PROPERTY(double priceNO4 READ priceNO4 NOTIFY changed)
    Q_PROPERTY(double priceNO5 READ priceNO5 NOTIFY changed)
    Q_PROPERTY(double systemPrice READ systemPrice NOTIFY changed)

    Q_PROPERTY(bool hasObservedPrices READ hasObservedPrices NOTIFY changed)
    Q_PROPERTY(bool hasObservedPriceNO1 READ hasObservedPriceNO1 NOTIFY changed)
    Q_PROPERTY(bool hasObservedPriceNO2 READ hasObservedPriceNO2 NOTIFY changed)
    Q_PROPERTY(bool hasObservedPriceNO3 READ hasObservedPriceNO3 NOTIFY changed)
    Q_PROPERTY(bool hasObservedPriceNO4 READ hasObservedPriceNO4 NOTIFY changed)
    Q_PROPERTY(bool hasObservedPriceNO5 READ hasObservedPriceNO5 NOTIFY changed)
    Q_PROPERTY(QVariantList observedPriceSeries READ observedPriceSeries NOTIFY changed)
    Q_PROPERTY(QString observedPriceDayStartUtc READ observedPriceDayStartUtc NOTIFY changed)
    Q_PROPERTY(QString observedPriceDayEndUtc READ observedPriceDayEndUtc NOTIFY changed)
    Q_PROPERTY(bool hasModelledPrices READ hasModelledPrices NOTIFY changed)
    Q_PROPERTY(QString modelledDeliveryTimeUtc READ modelledDeliveryTimeUtc NOTIFY changed)
    Q_PROPERTY(QString modelledDeliveryDayLocal READ modelledDeliveryDayLocal NOTIFY changed)
    Q_PROPERTY(QString modelledForecastOriginUtc READ modelledForecastOriginUtc NOTIFY changed)
    Q_PROPERTY(QVariantList modelledPriceSeriesA READ modelledPriceSeriesA NOTIFY changed)
    Q_PROPERTY(QVariantList modelledPriceSeriesB READ modelledPriceSeriesB NOTIFY changed)
    Q_PROPERTY(bool hasPhysicalFlows READ hasPhysicalFlows NOTIFY changed)
    Q_PROPERTY(bool hasFrequency READ hasFrequency NOTIFY changed)
    Q_PROPERTY(int physicalFlowCount READ physicalFlowCount NOTIFY changed)
    Q_PROPERTY(QVariantList physicalFlows READ physicalFlows NOTIFY changed)

    Q_PROPERTY(double obsPriceNO1 READ obsPriceNO1 NOTIFY changed)
    Q_PROPERTY(double obsPriceNO2 READ obsPriceNO2 NOTIFY changed)
    Q_PROPERTY(double obsPriceNO3 READ obsPriceNO3 NOTIFY changed)
    Q_PROPERTY(double obsPriceNO4 READ obsPriceNO4 NOTIFY changed)
    Q_PROPERTY(double obsPriceNO5 READ obsPriceNO5 NOTIFY changed)

    Q_PROPERTY(double modPriceNO1 READ modPriceNO1 NOTIFY changed)
    Q_PROPERTY(double modPriceNO2 READ modPriceNO2 NOTIFY changed)
    Q_PROPERTY(double modPriceNO3 READ modPriceNO3 NOTIFY changed)
    Q_PROPERTY(double modPriceNO4 READ modPriceNO4 NOTIFY changed)
    Q_PROPERTY(double modPriceNO5 READ modPriceNO5 NOTIFY changed)

    Q_PROPERTY(double flowNO1_NO2 READ flowNO1_NO2 NOTIFY changed)
    Q_PROPERTY(double flowNO1_NO3 READ flowNO1_NO3 NOTIFY changed)
    Q_PROPERTY(double flowNO1_NO5 READ flowNO1_NO5 NOTIFY changed)
    Q_PROPERTY(double flowNO2_NO5 READ flowNO2_NO5 NOTIFY changed)
    Q_PROPERTY(double flowNO3_NO4 READ flowNO3_NO4 NOTIFY changed)
    Q_PROPERTY(double flowNO3_NO5 READ flowNO3_NO5 NOTIFY changed)

    Q_PROPERTY(double netExportEuropeMW READ netExportEuropeMW NOTIFY changed)
    Q_PROPERTY(double mfrrActivatedMW READ mfrrActivatedMW NOTIFY changed)
    Q_PROPERTY(double shadowPriceNO1_NO3 READ shadowPriceNO1_NO3 NOTIFY changed)
    Q_PROPERTY(double shadowPriceNO3_NO4 READ shadowPriceNO3_NO4 NOTIFY changed)

    Q_PROPERTY(QString dataSourceMode READ dataSourceMode WRITE setDataSourceMode NOTIFY changed)
    Q_PROPERTY(QString activeScenario READ activeScenario WRITE setScenario NOTIFY changed)
    Q_PROPERTY(QString dayAheadMode READ dayAheadMode WRITE setDayAheadMode NOTIFY changed)
    Q_PROPERTY(QString sourceStatus READ sourceStatus NOTIFY changed)
    Q_PROPERTY(QString lastUpdatedUtc READ lastUpdatedUtc NOTIFY changed)
    Q_PROPERTY(QString bindingConstraint READ bindingConstraint NOTIFY changed)
    Q_PROPERTY(QString modelBasisExplanation READ modelBasisExplanation NOTIFY changed)
    Q_PROPERTY(double currentHourFraction READ currentHourFraction NOTIFY changed)
    Q_PROPERTY(quint64 timestampUs READ timestampUs NOTIFY changed)

public:
    explicit MetadataStore(QObject* parent = nullptr);

    double frequencyHz() const;
    double priceNO1() const;
    double priceNO2() const;
    double priceNO3() const;
    double priceNO4() const;
    double priceNO5() const;
    double systemPrice() const;

    bool hasObservedPrices() const;
    bool hasObservedPriceNO1() const;
    bool hasObservedPriceNO2() const;
    bool hasObservedPriceNO3() const;
    bool hasObservedPriceNO4() const;
    bool hasObservedPriceNO5() const;
    QVariantList observedPriceSeries() const;
    QString observedPriceDayStartUtc() const;
    QString observedPriceDayEndUtc() const;
    bool hasModelledPrices() const;
    QString modelledDeliveryTimeUtc() const;
    QString modelledDeliveryDayLocal() const;
    QString modelledForecastOriginUtc() const;
    QVariantList modelledPriceSeriesA() const;
    QVariantList modelledPriceSeriesB() const;
    bool hasPhysicalFlows() const;
    bool hasFrequency() const;
    int physicalFlowCount() const;
    QVariantList physicalFlows() const;

    double obsPriceNO1() const;
    double obsPriceNO2() const;
    double obsPriceNO3() const;
    double obsPriceNO4() const;
    double obsPriceNO5() const;

    double modPriceNO1() const;
    double modPriceNO2() const;
    double modPriceNO3() const;
    double modPriceNO4() const;
    double modPriceNO5() const;

    double flowNO1_NO2() const;
    double flowNO1_NO3() const;
    double flowNO1_NO5() const;
    double flowNO2_NO5() const;
    double flowNO3_NO4() const;
    double flowNO3_NO5() const;

    double netExportEuropeMW() const;
    double mfrrActivatedMW() const;
    double shadowPriceNO1_NO3() const;
    double shadowPriceNO3_NO4() const;

    QString dataSourceMode() const;
    QString activeScenario() const;
    QString dayAheadMode() const;
    QString sourceStatus() const;
    QString lastUpdatedUtc() const;
    QString bindingConstraint() const;
    QString modelBasisExplanation() const;
    double currentHourFraction() const;
    quint64 timestampUs() const;

    GridTelemetry currentTelemetry() const;

    Q_INVOKABLE bool lineIsBinding(const QString& lineId) const;
    Q_INVOKABLE double lineCapacity(const QString& lineId) const;
    Q_INVOKABLE double lineShadowPrice(const QString& lineId) const;

signals:
    void changed();
    void scenarioRequested(const QString& scenarioId);
    void dayAheadModeRequested(const QString& mode, const QString& scenarioId);

public slots:
    void update(const GridTelemetry& telemetry);
    void setScenario(const QString& scenarioId);
    void setDayAheadMode(const QString& mode);
    void setDataSourceMode(const QString& mode);
    void toggleDataSourceMode();
    void setSourceStatus(const QString& status);
    void reset();

private slots:
    void onTick();

private:
    void selectDisplayedPrices();

    mutable QMutex m_mutex;
    GridTelemetry m_data;
    QTimer m_tickTimer;
};
