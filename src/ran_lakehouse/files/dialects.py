"""Vendor dialect dictionaries (rule P5): vendor-style counter names and how
each is formed from the 3GPP measurements the model produces.

Dialects are "modelled on" public descriptions of vendor counters, never
vendor copies. Each entry records how its name is attested in public
sources, or ASSUMPTION where no public name was found; descriptions are
written independently. Dictionaries are versioned per software release
(rule D5): a release can rename a counter.

Huawei-style names are modelled on public third-party copies of vendor
KPI references and training material, and on a public paper:
- LTE: eNodeB KPI Reference (2012 baseline, re-hosted on SlideShare);
  iJOE vol. 14 no. 11 (2018), DOI 10.3991/ijoe.v14i11.9256, which prints
  L.Thrp.bits.DL, L.Thrp.Time.DL, L.ChMeas.PRB.DL.Used.Avg and
  L.ChMeas.PRB.DL.Avail; public engineering blogs for L.S1Sig.ConnEst.* and
  L.HHO.*.IntraFreq.Exec*Out.
- GSM: public GBSS KPI decks (K3000-series and CA30x counters, CM33 drops).
"""

from dataclasses import dataclass

ATTESTED = "public description"
WEAK = "public description, weakly attested"
ASSUMED = "ASSUMPTION: no public name found"


@dataclass(frozen=True)
class Entry:
    """One vendor-style counter.

    Attributes:
        name: Vendor-style counter name (an XML Name).
        rule: How the value is formed: "copy" (canonical times scale),
            "sum", "difference" (first minus the rest, not below 0),
            "prb_used" (PRB percentage of N_RB), "prb_avail" (N_RB),
            "relation_same_site" / "relation_other_site" (a per-relation
            counter summed over targets on the same or another site),
            "bin" (one bin of a distribution).
        canonical: The 3GPP measurement names it reads.
        scale: Factor for "copy".
        bin_index: Bin for "bin"; -1 otherwise.
        attestation: How the name is attested.
    """

    name: str
    rule: str
    canonical: tuple[str, ...]
    scale: float
    bin_index: int
    attestation: str


def copy(name: str, canonical: str, attestation: str, scale: float = 1.0) -> Entry:
    """An entry that copies one measurement.

    Args:
        name: Vendor-style name.
        canonical: 3GPP measurement.
        attestation: How the name is attested.
        scale: Unit factor.

    Returns:
        The entry.
    """
    return Entry(name, "copy", (canonical,), scale, -1, attestation)


def rule(name: str, kind: str, canonical: tuple[str, ...], attestation: str) -> Entry:
    """An entry formed by a rule other than copy.

    Args:
        name: Vendor-style name.
        kind: The rule.
        canonical: 3GPP measurements read.
        attestation: How the name is attested.

    Returns:
        The entry.
    """
    return Entry(name, kind, canonical, 1.0, -1, attestation)


def bins(prefix: str, canonical: str, count: int, attestation: str) -> list[Entry]:
    """Entries for every bin of a distribution.

    Args:
        prefix: Name prefix; the bin index is appended.
        canonical: 3GPP distribution measurement.
        count: Number of bins.
        attestation: How the names are attested.

    Returns:
        One entry per bin.
    """
    return [Entry(f"{prefix}{i}", "bin", (canonical,), 1.0, i, attestation) for i in range(count)]


# Distribution measurements report every 60 minutes, the rest every 15
# (ASSUMPTION, rules P1 and P4: distributions are the bulk of the values
# and vendors commonly collect them at a coarser granularity).
HOURLY_GROUPS = frozenset({"LTE.CQI", "LTE.TA", "LTE_Quality_DL", "LTE_Timing_Advance"})


@dataclass(frozen=True)
class MeasGroup:
    """One measInfo of a dialect: a measured object class and its counters.

    Attributes:
        meas_info_id: measInfoId in the file.
        technology: "LTE" or "GSM".
        objects: "cell" or "relation".
        entries: The counters.
    """

    meas_info_id: str
    technology: str
    objects: str
    entries: tuple[Entry, ...]

    @property
    def granularity_min(self) -> int:
        """Reporting granularity of the group, minutes.

        Returns:
            60 for distribution measurements, else 15.
        """
        return 60 if self.meas_info_id in HOURLY_GROUPS else 15


@dataclass(frozen=True)
class Dialect:
    """A vendor dialect at one software release.

    Attributes:
        vendor: "huawei" or "nokia" (the vendor region of the world).
        release: Software release label (synthetic).
        groups: measInfos in file order.
    """

    vendor: str
    release: str
    groups: tuple[MeasGroup, ...]


CQI_BINS = 16  # TS 36.213 Table 7.2.3-1 CQI 0-15
TA_BINS = 15  # the model's TA distance bins (serving.TA_BIN_EDGES_STEPS)

HUAWEI_R1 = Dialect(
    vendor="huawei",
    release="HW-R1",
    groups=(
        MeasGroup(
            "LTE.Cell",
            "LTE",
            "cell",
            (
                copy("L.RRC.ConnReq.Att", "RRC.ConnEstabAtt.sum", ATTESTED),
                copy("L.RRC.ConnReq.Succ", "RRC.ConnEstabSucc.sum", ATTESTED),
                copy("L.S1Sig.ConnEst.Att", "S1SIG.ConnEstabAtt", ATTESTED),
                copy("L.S1Sig.ConnEst.Succ", "S1SIG.ConnEstabSucc", ATTESTED),
                copy("L.E-RAB.AttEst", "ERAB.EstabInitAttNbr.sum", ATTESTED),
                copy("L.E-RAB.SuccEst", "ERAB.EstabInitSuccNbr.sum", ATTESTED),
                copy("L.E-RAB.AbnormRel", "ERAB.RelActNbr.sum", ATTESTED),
                rule(
                    "L.E-RAB.NormRel",
                    "difference",
                    ("ERAB.EstabInitSuccNbr.sum", "ERAB.RelActNbr.sum"),
                    ATTESTED,
                ),
                # kbit (TS 32.425) to bits.
                copy("L.Thrp.bits.DL", "DRB.IPVolDl.sum", ATTESTED, scale=1000.0),
                copy("L.Thrp.Time.DL", "DRB.IPTimeDl.sum", ATTESTED),
                rule("L.ChMeas.PRB.DL.Used.Avg", "prb_used", ("RRU.PrbTotDl",), ATTESTED),
                rule("L.ChMeas.PRB.DL.Avail", "prb_avail", (), ATTESTED),
                copy("L.Traffic.User.Avg", "RRC.ConnMean", ATTESTED),
                copy("L.Traffic.User.Max", "RRC.ConnMax", ATTESTED),
                rule(
                    "L.HHO.IntraeNB.IntraFreq.ExecAttOut",
                    "relation_same_site",
                    ("HO.OutAttTarget.sum",),
                    ATTESTED,
                ),
                rule(
                    "L.HHO.IntraeNB.IntraFreq.ExecSuccOut",
                    "relation_same_site",
                    ("HO.OutSuccTarget.sum",),
                    ATTESTED,
                ),
                rule(
                    "L.HHO.IntereNB.IntraFreq.ExecAttOut",
                    "relation_other_site",
                    ("HO.OutAttTarget.sum",),
                    ATTESTED,
                ),
                rule(
                    "L.HHO.IntereNB.IntraFreq.ExecSuccOut",
                    "relation_other_site",
                    ("HO.OutSuccTarget.sum",),
                    ATTESTED,
                ),
                copy("L.Cell.Unavail.Dur.Sys", "RRU.CellUnavailableTime.sum", ATTESTED),
                copy(
                    "L.UL.Interference.Avg", "UL interference per PRB, dBm (vendor-style)", ASSUMED
                ),
            ),
        ),
        MeasGroup(
            "LTE.CQI",
            "LTE",
            "cell",
            tuple(bins("L.ChMeas.CQI.DL.", "CARR.WBCQIDist.Bin", CQI_BINS, WEAK)),
        ),
        MeasGroup(
            "LTE.TA",
            "LTE",
            "cell",
            # Bin edges are the model's (serving.TA_BIN_EDGES_STEPS), ASSUMPTION.
            tuple(bins("L.RA.TA.UE.Index", "TA distance bins (vendor-style)", TA_BINS, WEAK)),
        ),
        MeasGroup(
            "LTE.NCell",
            "LTE",
            "relation",
            (
                copy("L.HHO.NCell.ExecAttOut", "HO.OutAttTarget.sum", ASSUMED),
                copy("L.HHO.NCell.ExecSuccOut", "HO.OutSuccTarget.sum", ASSUMED),
            ),
        ),
        MeasGroup(
            "GSM.Cell",
            "GSM",
            "cell",
            (
                # Requests include the ones that met all TCHs busy.
                rule(
                    "K3010A",
                    "sum",
                    ("attTCHSeizures", "attTCHSeizuresMeetingTCHBlockedState"),
                    ATTESTED,
                ),
                copy("K3011A", "attTCHSeizuresMeetingTCHBlockedState", ATTESTED),
                copy("K3013A", "succTCHSeizures", ATTESTED),
                copy("K3014", "meanNbrOfBusyTCHs", ATTESTED),
                copy("K3015", "nbrOfAvailableTCHs", ATTESTED),
                copy("CA300J", "attImmediateAssingProcs", ATTESTED),
                copy("CA301J", "succImmediateAssingProcs", ATTESTED),
                copy("K3001", "attSDCCHSeizuresMeetingSDCCHBlockedState", ATTESTED),
                copy("K3004", "meanNbrOfBusySDCCHs", ATTESTED),
                copy("CM33", "nbrOfLostRadioLinksTCH", ATTESTED),
                copy("H3121Ca", "attOutgoingInternalInterCellHDOs", ASSUMED),
                copy("H3123Ca", "succOutgoingInternalInterCellHDOs", ASSUMED),
                rule(
                    "H3127Ca",
                    "sum",
                    ("unsuccHDOsWithReconnection", "unsuccHDOsWithLossOfConnection"),
                    ATTESTED,
                ),
                copy("H3023Ca", "succIncomingInternalInterCellHDOs", ASSUMED),
            ),
        ),
        MeasGroup(
            "GSM.NCell",
            "GSM",
            "relation",
            (
                copy("H3121Cb", "attOutgoingInternalInterCellHDOsPerTargetCell", ASSUMED),
                copy("H3123Cb", "succOutgoingInternalInterCellHDOsPerTargetCell", ASSUMED),
            ),
        ),
    ),
)


# Nokia-style ids are modelled on public third-party copies of vendor
# training material (M<measurement>C<counter> ids: M8006 EPS bearer, M8010
# DL quality, M8011 cell resources, M8013 signalling, M8020 availability,
# M8015 per-neighbour handover) and on public BSS formula decks (numeric
# BSS counter ids such as 1026 tch_call_req). Where no public id was found
# the id is an ASSUMPTION in the same style.
SAMPLES_PER_PERIOD = 90  # availability samples per 15 min, one per 10 s (ASSUMPTION)

NOKIA_R1 = Dialect(
    vendor="nokia",
    release="NK-R1",
    groups=(
        MeasGroup(
            "LTE_Signalling",
            "LTE",
            "cell",
            (
                copy("M8013C17", "RRC.ConnEstabAtt.sum", WEAK),
                copy("M8013C5", "RRC.ConnEstabSucc.sum", WEAK),
                copy("M8013C43", "S1SIG.ConnEstabAtt", ATTESTED),
                copy("M8013C44", "S1SIG.ConnEstabSucc", ATTESTED),
            ),
        ),
        MeasGroup(
            "LTE_EPS_Bearer",
            "LTE",
            "cell",
            (
                copy("M8006C0", "ERAB.EstabInitAttNbr.sum", ATTESTED),
                copy("M8006C1", "ERAB.EstabInitSuccNbr.sum", ATTESTED),
                copy("M8006C176", "ERAB.RelActNbr.sum", ASSUMED),
                copy("M8006C181", "ERAB.SessionTimeUE", ASSUMED),
            ),
        ),
        MeasGroup(
            "LTE_Cell_Throughput",
            "LTE",
            "cell",
            (
                # kbit (TS 32.425) to kByte.
                copy("M8012C20", "DRB.IPVolDl.sum", WEAK, scale=1.0 / 8.0),
                copy("M8012C92", "DRB.IPTimeDl.sum", ASSUMED),
            ),
        ),
        MeasGroup(
            "LTE_Cell_Resource",
            "LTE",
            "cell",
            (copy("M8011C37", "RRU.PrbTotDl", ATTESTED),),
        ),
        MeasGroup(
            "LTE_Cell_Load",
            "LTE",
            "cell",
            (
                copy("M8001C199", "RRC.ConnMean", ASSUMED),
                copy("M8001C200", "RRC.ConnMax", ASSUMED),
            ),
        ),
        MeasGroup(
            "LTE_Cell_Avail",
            "LTE",
            "cell",
            (
                rule("M8020C3", "availability_samples", ("RRU.CellUnavailableTime.sum",), ATTESTED),
                rule("M8020C6", "samples_total", (), ASSUMED),
            ),
        ),
        MeasGroup(
            "LTE_Intra_Freq_HO",
            "LTE",
            "cell",
            (
                copy("M8014C19", "HO.IntraFreqOutAtt", ASSUMED),
                copy("M8014C20", "HO.IntraFreqOutSucc", ASSUMED),
            ),
        ),
        MeasGroup(
            "LTE_Power_Quality_UL",
            "LTE",
            "cell",
            (copy("M8005C306", "UL interference per PRB, dBm (vendor-style)", ASSUMED),),
        ),
        MeasGroup(
            "LTE_Quality_DL",
            "LTE",
            "cell",
            # M8010C36-C51: the CQI 0-15 histogram.
            tuple(
                Entry(f"M8010C{36 + i}", "bin", ("CARR.WBCQIDist.Bin",), 1.0, i, ATTESTED)
                for i in range(CQI_BINS)
            ),
        ),
        MeasGroup(
            "LTE_Timing_Advance",
            "LTE",
            "cell",
            tuple(
                Entry(f"M8029C{i}", "bin", ("TA distance bins (vendor-style)",), 1.0, i, ASSUMED)
                for i in range(TA_BINS)
            ),
        ),
        MeasGroup(
            "LTE_Neighb_Cell_HO",
            "LTE",
            "relation",
            (
                copy("M8015C9", "HO.OutAttTarget.sum", ASSUMED),
                copy("M8015C10", "HO.OutSuccTarget.sum", ASSUMED),
            ),
        ),
        MeasGroup(
            "BSC_Traffic",
            "GSM",
            "cell",
            (
                # tch_call_req counts requests, including the blocked ones.
                rule(
                    "c1026",
                    "sum",
                    ("attTCHSeizures", "attTCHSeizuresMeetingTCHBlockedState"),
                    ATTESTED,
                ),
                copy("c1009", "succTCHSeizures", ATTESTED),
                copy("c1010", "attTCHSeizuresMeetingTCHBlockedState", ASSUMED),
                copy("c1000", "attImmediateAssingProcs", ATTESTED),
                copy("c1001", "attSDCCHSeizuresMeetingSDCCHBlockedState", ATTESTED),
                copy("c57021", "succImmediateAssingProcs", ATTESTED),
                copy("c1003", "nbrOfLostRadioLinksSDCCH", ATTESTED),
                copy("c1013", "nbrOfLostRadioLinksTCH", ASSUMED),
                copy("c1120", "nbrOfAvailableTCHs", ASSUMED),
                copy("c1121", "meanNbrOfBusyTCHs", ASSUMED),
            ),
        ),
        MeasGroup(
            "BSC_Handover",
            "GSM",
            "cell",
            (
                copy("c4000", "attOutgoingInternalInterCellHDOs", ASSUMED),
                copy("c4004", "succOutgoingInternalInterCellHDOs", ASSUMED),
                copy("c4010", "succIncomingInternalInterCellHDOs", ASSUMED),
            ),
        ),
        MeasGroup(
            "BSC_Adjacent_HO",
            "GSM",
            "relation",
            (
                copy("c4100", "attOutgoingInternalInterCellHDOsPerTargetCell", ASSUMED),
                copy("c4104", "succOutgoingInternalInterCellHDOsPerTargetCell", ASSUMED),
            ),
        ),
    ),
)


# Huawei-style release HW-R2 (rule D5): a software upgrade renames a few
# counters; the meaning and the values stay the same. The new names are
# ASSUMPTION, in the dialect's style.
HW_R2_RENAMES = {
    "L.Thrp.bits.DL": "L.Thrp.bits.DL.Total",
    "L.E-RAB.AbnormRel": "L.E-RAB.AbnormRel.Sum",
    "L.Traffic.User.Avg": "L.Traffic.ActiveUser.Avg",
}


def renamed(dialect: Dialect, release: str, renames: dict[str, str]) -> Dialect:
    """A later release of a dialect with some counters renamed.

    Args:
        dialect: The earlier release.
        release: The new release label.
        renames: Old name to new name.

    Returns:
        The new release.

    Raises:
        ValueError: If a name to rename is not in the dialect.
    """
    names = {e.name for g in dialect.groups for e in g.entries}
    missing = set(renames) - names
    if missing:
        raise ValueError(f"cannot rename counters not in {dialect.release}: {sorted(missing)}")
    groups = tuple(
        MeasGroup(
            g.meas_info_id,
            g.technology,
            g.objects,
            tuple(
                Entry(
                    renames.get(e.name, e.name),
                    e.rule,
                    e.canonical,
                    e.scale,
                    e.bin_index,
                    ASSUMED if e.name in renames else e.attestation,
                )
                for e in g.entries
            ),
        )
        for g in dialect.groups
    )
    return Dialect(dialect.vendor, release, groups)


HUAWEI_R2 = renamed(HUAWEI_R1, "HW-R2", HW_R2_RENAMES)
RELEASES = {d.release: d for d in (HUAWEI_R1, HUAWEI_R2, NOKIA_R1)}
