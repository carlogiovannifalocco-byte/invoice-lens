"""Generate fake FatturaPA 1.2 invoices for the demo. All companies and VAT numbers are invented."""

from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "samples"

CUSTOMER = ("Giulia", "Rossi", "01234567890")  # the fictional freelancer receiving the invoices

# supplier, vat, number, date, due date, [(description, amount, vat rate)], tax override
SAMPLES = [
    ("Nuvola Hosting S.r.l.", "10000000001", "2026/0412", "2026-10-01", "2026-10-31", [("Hosting VPS annuale", 240.00, 22), ("Dominio .it rinnovo", 12.00, 22)], None),
    ("Rotaie Veloci S.p.A.", "10000000002", "RV-88213", "2026-10-03", "", [("Biglietto treno alta velocita Roma-Milano", 81.97, 10)], None),
    ("Studio Bianchi Commercialisti", "10000000003", "57", "2026-10-05", "2026-11-05", [("Consulenza contabile trimestrale", 450.00, 22)], None),
    ("FibraVeloce S.p.A.", "10000000004", "FV2026-09981", "2026-10-06", "2026-10-20", [("Canone fibra ufficio ottobre", 24.59, 22)], None),
    ("Pixel Store S.r.l.", "10000000005", "PS-1190", "2026-10-08", "2026-10-08", [("Monitor 27 pollici", 290.00, 22), ("Tastiera meccanica", 89.00, 22)], None),
    ("Coworking Dorsoduro S.r.l.", "10000000006", "CW-310", "2026-10-09", "2026-10-15", [("Postazione coworking mensile", 180.00, 22)], None),
    ("Accademia Digitale S.r.l.", "10000000007", "AD/2026/77", "2026-10-10", "2026-11-10", [("Corso online sviluppo AI", 199.00, 22)], 40.00),
    ("Nuvola Hosting S.r.l.", "10000000001", "2026/0412", "2026-10-12", "2026-10-31", [("Hosting VPS annuale", 240.00, 22), ("Dominio .it rinnovo", 12.00, 22)], None),
    ("Energia Chiara S.p.A.", "10000000008", "EC-554433", "2026-09-28", "2026-10-14", [("Fornitura energia elettrica settembre", 63.11, 22)], None),
    ("Trattoria da Mario di Mario Verdi", "10000000009", "212", "2026-10-14", "", [("Pranzo di lavoro con cliente", 54.55, 10)], None),
]


def money(x: float) -> str:
    return f"{x:.2f}"


def build(supplier, vat, number, day, due, lines, tax_override) -> str:
    by_rate: dict[int, float] = {}
    rows = []
    for i, (desc, amount, rate) in enumerate(lines, 1):
        by_rate[rate] = by_rate.get(rate, 0) + amount
        rows.append(
            f"""      <DettaglioLinee>
        <NumeroLinea>{i}</NumeroLinea>
        <Descrizione>{desc}</Descrizione>
        <Quantita>1.00</Quantita>
        <PrezzoUnitario>{money(amount)}</PrezzoUnitario>
        <PrezzoTotale>{money(amount)}</PrezzoTotale>
        <AliquotaIVA>{rate:.2f}</AliquotaIVA>
      </DettaglioLinee>"""
        )
    summaries, total = [], 0.0
    for rate, taxable in by_rate.items():
        tax = tax_override if tax_override is not None else round(taxable * rate / 100, 2)
        total += taxable + tax
        summaries.append(
            f"""      <DatiRiepilogo>
        <AliquotaIVA>{rate:.2f}</AliquotaIVA>
        <ImponibileImporto>{money(taxable)}</ImponibileImporto>
        <Imposta>{money(tax)}</Imposta>
      </DatiRiepilogo>"""
        )
    payment = (
        f"""    <DatiPagamento>
      <CondizioniPagamento>TP02</CondizioniPagamento>
      <DettaglioPagamento>
        <ModalitaPagamento>MP05</ModalitaPagamento>
        <DataScadenzaPagamento>{due}</DataScadenzaPagamento>
        <ImportoPagamento>{money(total)}</ImportoPagamento>
      </DettaglioPagamento>
    </DatiPagamento>
"""
        if due
        else ""
    )
    nl = "\n"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<p:FatturaElettronica versione="FPR12" xmlns:p="http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2">
  <FatturaElettronicaHeader>
    <DatiTrasmissione>
      <IdTrasmittente><IdPaese>IT</IdPaese><IdCodice>{vat}</IdCodice></IdTrasmittente>
      <ProgressivoInvio>{number.replace('/', '')[-5:]}</ProgressivoInvio>
      <FormatoTrasmissione>FPR12</FormatoTrasmissione>
      <CodiceDestinatario>0000000</CodiceDestinatario>
    </DatiTrasmissione>
    <CedentePrestatore>
      <DatiAnagrafici>
        <IdFiscaleIVA><IdPaese>IT</IdPaese><IdCodice>{vat}</IdCodice></IdFiscaleIVA>
        <Anagrafica><Denominazione>{supplier}</Denominazione></Anagrafica>
        <RegimeFiscale>RF01</RegimeFiscale>
      </DatiAnagrafici>
    </CedentePrestatore>
    <CessionarioCommittente>
      <DatiAnagrafici>
        <IdFiscaleIVA><IdPaese>IT</IdPaese><IdCodice>{CUSTOMER[2]}</IdCodice></IdFiscaleIVA>
        <Anagrafica><Nome>{CUSTOMER[0]}</Nome><Cognome>{CUSTOMER[1]}</Cognome></Anagrafica>
      </DatiAnagrafici>
    </CessionarioCommittente>
  </FatturaElettronicaHeader>
  <FatturaElettronicaBody>
    <DatiGenerali>
      <DatiGeneraliDocumento>
        <TipoDocumento>TD01</TipoDocumento>
        <Divisa>EUR</Divisa>
        <Data>{day}</Data>
        <Numero>{number}</Numero>
        <ImportoTotaleDocumento>{money(total)}</ImportoTotaleDocumento>
      </DatiGeneraliDocumento>
    </DatiGenerali>
    <DatiBeniServizi>
{nl.join(rows)}
{nl.join(summaries)}
    </DatiBeniServizi>
{payment}  </FatturaElettronicaBody>
</p:FatturaElettronica>
"""


def main():
    OUT.mkdir(exist_ok=True)
    for i, sample in enumerate(SAMPLES, 1):
        slug = sample[0].split()[0].lower()
        (OUT / f"{i:02d}-{slug}.xml").write_text(build(*sample), encoding="utf-8")
    print(f"wrote {len(SAMPLES)} invoices to {OUT}")


if __name__ == "__main__":
    main()
