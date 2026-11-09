"""What is loaded from each AEMO table, and what is deliberately left out.

Columns are read by name. Each spec keeps only what a rung of the ladder uses or will use.
"""
from __future__ import annotations

from nemsim.mmsdm import Table

TS, TXT, F, I = "timestamp", "varchar", "double", "integer"

BANDS = {f"BANDAVAIL{i}": F for i in range(1, 11)}
PRICES = {f"PRICEBAND{i}": F for i in range(1, 11)}

TABLES = {t.name: t for t in [
    # Final energy offers: MW per band per 5-minute interval. FCAS bid types are left out
    # (about 70% of the rows); no rung models FCAS yet.
    Table("BIDPEROFFER_D", {"SETTLEMENTDATE": TS, "DUID": TXT, "BIDTYPE": TXT, "DIRECTION": TXT,
                            "INTERVAL_DATETIME": TS, "OFFERDATE": TS, "PERIODID": I, "MAXAVAIL": F,
                            "FIXEDLOAD": F, "ROCUP": F, "ROCDOWN": F, **BANDS, "PASAAVAILABILITY": F},
          where="BIDTYPE = 'ENERGY'",
          # DIRECTION arrived with bidirectional units (3 June 2024). Older files lack it; nemsim/add_month.py
          # fills it from the unit's dispatch type for offers before that date, and only those.
          optional=frozenset({"DIRECTION"})),
    # Band prices per trading day. REBIDEXPLANATION (free text about named firms) is left out.
    Table("BIDDAYOFFER_D", {"SETTLEMENTDATE": TS, "DUID": TXT, "BIDTYPE": TXT, "DIRECTION": TXT,
                            "OFFERDATE": TS, "VERSIONNO": I, "PARTICIPANTID": TXT,
                            "DAILYENERGYCONSTRAINT": F, **PRICES, "MINIMUMLOAD": F,
                            "T1": F, "T2": F, "T3": F, "T4": F, "ENTRYTYPE": TXT},
          where="BIDTYPE = 'ENERGY'", optional=frozenset({"DIRECTION"})),
    # Unit dispatch solution: availability, initial MW, ramp rates (MW per hour), cleared MW and
    # battery stored energy, and FCAS enablement for diagnostics. The FCAS trapezium columns are left out.
    Table("DISPATCHLOAD", {"SETTLEMENTDATE": TS, "DUID": TXT, "INTERVENTION": I, "DISPATCHMODE": I,
                           "AGCSTATUS": I, "INITIALMW": F, "TOTALCLEARED": F, "RAMPDOWNRATE": F,
                           "RAMPUPRATE": F, "AVAILABILITY": F, "SEMIDISPATCHCAP": I, "UIGF": F,
                           "CONFORMANCE_MODE": I, "MIN_AVAILABILITY": F,
                           # Batteries: stored energy (MWh) at the start and end of the interval.
                           "INITIAL_ENERGY_STORAGE": F, "ENERGY_STORAGE": F,
                           # FCAS enablement, MW: an outcome of the joint energy-FCAS solve. Loaded only
                           # to measure how much energy FCAS holds back; no rung reads it.
                           **{f"{d}{s}": F for d in ("RAISE", "LOWER") for s in ("REG", "1SEC", "6SEC", "60SEC", "5MIN")}},
          # Older layouts (2020) lack the battery, 1-second FCAS, UIGF, conformance and minimum-availability
          # columns. No rung reads UIGF directly: rung 2 caps at AVAILABILITY, which equals min(UIGF, MAXAVAIL)
          # for 100% of 385,247 semi-scheduled unit-intervals tested (10-17 September 2025).
          optional=frozenset({"INITIAL_ENERGY_STORAGE", "ENERGY_STORAGE", "RAISE1SEC", "LOWER1SEC",
                              "UIGF", "CONFORMANCE_MODE", "MIN_AVAILABILITY"})),
    # Every invoked constraint, every interval: right-hand side, marginal value, LHS.
    Table("DISPATCHCONSTRAINT", {"SETTLEMENTDATE": TS, "CONSTRAINTID": TXT, "INTERVENTION": I, "RHS": F,
                                 "MARGINALVALUE": F, "VIOLATIONDEGREE": F, "LHS": F,
                                 "GENCONID_EFFECTIVEDATE": TS, "GENCONID_VERSIONNO": I}),
    # Generic constraint definitions (rung 4). Each monthly file holds the versions created that month, so
    # the months fetched are worked out from the versions DISPATCHCONSTRAINT references (nemsim/definitions.py).
    Table("GENCONDATA", {"EFFECTIVEDATE": TS, "VERSIONNO": I, "GENCONID": TXT, "CONSTRAINTTYPE": TXT,
                         "CONSTRAINTVALUE": F, "GENERICCONSTRAINTWEIGHT": F, "DISPATCH": TXT, "LIMITTYPE": TXT,
                         "LASTCHANGED": TS}, optional=frozenset({"LIMITTYPE"})),
    Table("SPDCONNECTIONPOINTCONSTRAINT", {"CONNECTIONPOINTID": TXT, "EFFECTIVEDATE": TS, "VERSIONNO": I,
                                           "GENCONID": TXT, "FACTOR": F, "BIDTYPE": TXT, "LASTCHANGED": TS}),
    Table("SPDINTERCONNECTORCONSTRAINT", {"INTERCONNECTORID": TXT, "EFFECTIVEDATE": TS, "VERSIONNO": I,
                                          "GENCONID": TXT, "FACTOR": F, "LASTCHANGED": TS}),
    Table("SPDREGIONCONSTRAINT", {"REGIONID": TXT, "EFFECTIVEDATE": TS, "VERSIONNO": I, "GENCONID": TXT,
                                  "FACTOR": F, "BIDTYPE": TXT, "LASTCHANGED": TS}),
    # Interconnector loss model, for a later rung. Loaded whole; they are tiny.
    Table("LOSSMODEL", {"EFFECTIVEDATE": TS, "VERSIONNO": I, "INTERCONNECTORID": TXT, "PERIODID": I, "LOSSSEGMENT": I,
                        "MWBREAKPOINT": F, "LOSSFACTOR": F}),
    Table("LOSSFACTORMODEL", {"EFFECTIVEDATE": TS, "VERSIONNO": I, "INTERCONNECTORID": TXT,
                              "REGIONID": TXT, "DEMANDCOEFFICIENT": F}),
    Table("INTERCONNECTORCONSTRAINT", {"EFFECTIVEDATE": TS, "VERSIONNO": I, "INTERCONNECTORID": TXT,
                                       "FROMREGIONLOSSSHARE": F, "LOSSCONSTANT": F, "LOSSFLOWCOEFFICIENT": F,
                                       "IMPORTLIMIT": F, "EXPORTLIMIT": F, "ICTYPE": TXT}),

    # From the explainer's raw directory. ROP and the extra columns are why these are reloaded
    # here rather than read from its warehouse, which keeps only RRP and a few flags.
    Table("DISPATCHPRICE", {"SETTLEMENTDATE": TS, "REGIONID": TXT, "INTERVENTION": I, "RRP": F, "ROP": F,
                            "APCFLAG": I, "MARKETSUSPENDEDFLAG": I, "PRICE_STATUS": TXT}),
    Table("DISPATCHREGIONSUM", {"SETTLEMENTDATE": TS, "REGIONID": TXT, "INTERVENTION": I, "TOTALDEMAND": F,
                                "AVAILABLEGENERATION": F, "DISPATCHABLEGENERATION": F, "DISPATCHABLELOAD": F,
                                "NETINTERCHANGE": F, "AGGREGATEDISPATCHERROR": F, "INITIALSUPPLY": F,
                                "CLEAREDSUPPLY": F, "UIGF": F, "SS_SOLAR_UIGF": F, "SS_WIND_UIGF": F},
          optional=frozenset({"SS_SOLAR_UIGF", "SS_WIND_UIGF"})),
    Table("DISPATCHINTERCONNECTORRES", {"SETTLEMENTDATE": TS, "INTERCONNECTORID": TXT, "INTERVENTION": I,
                                        "METEREDMWFLOW": F, "MWFLOW": F, "MWLOSSES": F, "MARGINALVALUE": F,
                                        "VIOLATIONDEGREE": F, "EXPORTLIMIT": F, "IMPORTLIMIT": F,
                                        "MARGINALLOSS": F, "EXPORTGENCONID": TXT, "IMPORTGENCONID": TXT}),
    Table("DUDETAILSUMMARY", {"DUID": TXT, "START_DATE": TS, "END_DATE": TS, "DISPATCHTYPE": TXT,
                              "CONNECTIONPOINTID": TXT, "REGIONID": TXT, "STATIONID": TXT, "PARTICIPANTID": TXT,
                              "TRANSMISSIONLOSSFACTOR": F, "DISTRIBUTIONLOSSFACTOR": F,
                              "MINIMUM_ENERGY_PRICE": F, "MAXIMUM_ENERGY_PRICE": F, "SCHEDULE_TYPE": TXT,
                              "MAX_RAMP_RATE_UP": F, "MAX_RAMP_RATE_DOWN": F, "SECONDARY_TLF": F,
                              "DISPATCHSUBTYPE": TXT, "LASTCHANGED": TS}),
    Table("MARKET_PRICE_THRESHOLDS", {"EFFECTIVEDATE": TS, "VERSIONNO": I, "VOLL": F, "MARKETPRICEFLOOR": F,
                                      "ADMINISTERED_PRICE_THRESHOLD": F}),
]}
