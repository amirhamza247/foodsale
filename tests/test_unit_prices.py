"""Deterministic unit-price checks; no network or browser is used.
Run: python -m unittest discover -s tests -v
"""
import copy
import datetime as dt
from decimal import Decimal
from fractions import Fraction
import html
import importlib.util
from pathlib import Path
import sys
import unittest

spec = importlib.util.spec_from_file_location('foodsale_unit_tests_app', Path(__file__).resolve().parents[1] / 'app.py')
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


def offer(price, volume, published=''):
    return dict(name='Testvara', brand='Test', volume=volume, unit_price_text=published,
                price=price, ordinary='', member=False, eligibility='GENERAL', description='', image='',
                conditions=[], start='2026-11-16', end='2026-11-22')


class UnitPrices(unittest.TestCase):
    def test_card_price_order_and_separate_reference_quote(self):
        item = offer('9,90 kr/st', '500g', '19,80/kg')
        item.update(ordinary='26,97 kr/st', discount_percent=63.29, saving=17.07, saving_unit='st')
        markup = app.card(item, dt.date(2026, 11, 21))
        labels = ['class="offer-price">9,90 kr/st', '<strong>19,80 kr/kg', 'Ordinarie pris 26,97 kr/st',
                  'Jmf-pris 53,94 kr/kg', '<strong>Besparing</strong> 17,07 kr/st']
        positions = [markup.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        missing = app.card(offer('10 kr', ''), dt.date(2026, 11, 21))
        for label in ('Ordinarie pris saknas', 'Ordinarie jämförpris saknas', 'Besparing saknas'):
            self.assertNotIn(label, missing)

    def test_compact_card_keeps_limits_and_removes_duplicate_prices(self):
        item = offer('159/kg', '', '159:00 kr/kg')
        item.update(ordinary='175,00 kr/kg', member=True, discount_percent=9.14, saving=16, saving_unit='kg',
            conditions=['Max 3 köp', '159:00 kr/kg', 'Medlemspris', 'Lägsta pris 30 dagar: 175,00 kr/kg', 'Välj & blanda!'])
        markup = app.card(item, dt.date(2026, 11, 21))
        self.assertIn('159,00 kr/kg</strong><span class="inline-terms"> • Max 3 köp</span>', markup)
        self.assertIn('aria-label="Jämförpris: 159,00 kr/kg • Max 3 köp"', markup)
        self.assertEqual(markup.count('Max 3 köp'), 2)  # Visible text and screen-reader name.
        self.assertNotIn('<div>159:00 kr/kg</div>', markup)
        self.assertNotIn('<div>Medlemspris</div>', markup)
        self.assertIn('Lägsta 30-dgrspris 175,00 kr/kg', markup)
        self.assertIn('Välj &amp; blanda!', markup)
        self.assertIn('Willys Plus', markup)
        self.assertIn('Ordinarie pris 175,00 kr/kg', markup)
        self.assertIn('Jmf-pris 175,00 kr/kg', markup)

    def test_multibuy_shows_conditional_each_price(self):
        item = offer('2 för 35,00 kr', '400g')
        markup = app.card(item, dt.date(2026, 11, 21))
        self.assertIn('<strong>17,50 kr/st</strong> vid köp av 2', markup)
        self.assertLess(markup.index('class="offer-price"'), markup.index('class="ordinary multibuy-each"'))
        self.assertLess(markup.index('class="ordinary multibuy-each"'), markup.index('class="card-unit"'))
        self.assertIn('43,75 kr/kg', markup)
        for price in ('35,00 kr/st', '2 för 35 kr/st', 'från 2 för 35 kr'):
            self.assertNotIn('class="ordinary multibuy-each"', app.card(offer(price,'400g'),dt.date(2026,11,21)))
        markup = app.card(offer('3 för 100 kr +pant','33cl'),dt.date(2026,11,21))
        self.assertIn('33,33 kr/st</strong> vid köp av 3', markup)
        self.assertIn('Pant tillkommer', markup)

    def test_campaign_label_removed_and_discount_badge_restored(self):
        item = offer('10 kr', '500g')
        item.update(discount_percent=25, saving=3.33, saving_unit='st', conditions=['Kampanj', 'Max 3 köp', 'Gäller ej andra kampanjvaror'])
        markup = app.card(item, dt.date(2026, 11, 21))
        self.assertIn('<span class="badge discount">−25 %</span>', markup)
        self.assertNotIn('<div>Kampanj</div>', markup)
        self.assertIn('Max 3 köp', markup)
        self.assertIn('Gäller ej andra kampanjvaror', markup)
        for discount in (None, 0, -1, 101):
            self.assertNotIn('badge discount', app.card(dict(item,discount_percent=discount),dt.date(2026,11,21)))

    def test_retailer_formats_and_same_value_on_both_faces(self):
        cases = [
            ('2 för 35,00', '500g', '35:00 kr/kg', '35,00 kr/kilo'),
            ('29,90/st', '720g', '41:53 kr/kg', '41,53 kr/kilo'),
            ('39,95', '500ml', '79,90/l', '79,90 kr/liter'),
            ('10,00 kr/st', '750 g.', '13,33/kg.', '13,33 kr/kilo'),
            ('2 för 32 kr', '200g', '80:00/kg', '80,00 kr/kilo'),
            ('29,90 kr', '375g', '79,73 kr/kg', '79,73 kr/kilo'),
            ('2 för 79 kr', '500g', '79 kr/kg', '79,00 kr/kilo'),
            ('109/kg', '', '109:00 kr/kg', '109,00 kr/kilo'),
            ('79,90/st', '30p', '2:66 kr/st', '2,66 kr/styck'),
            ('59,90/st +pant', '15p/33cl', '12:10 kr/l +pant', '12,10 kr/liter'),
            ('19,90', '1, 5 l', '', '13,27 kr/liter'),
        ]
        for price, volume, published, expected in cases:
            with self.subTest(price=price, volume=volume):
                item = offer(price, volume, published)
                untouched = copy.deepcopy(item)
                front = app.offer_unit_quote(item)
                back = app.with_unit_price(app.comparison_offer_row('willys-oster', item, dt.datetime.now(app.STOCKHOLM).isoformat()), volume, item['name'])
                self.assertEqual(front['unitPriceDisplay'], expected)
                self.assertEqual(back['unitPriceDisplay'], expected)
                markup = app.card(item, dt.date(2026, 11, 21))
                self.assertIn('<strong>' + html.escape(expected.replace('/kilo', '/kg').replace('/liter', '/l').replace('/styck', '/st')) + '</strong>', markup)
                self.assertEqual(item, untouched)

    def test_integer_fraction_oracle_for_392_quotes(self):
        # Independent rational arithmetic calculates the expected number of öre per base unit.
        packs = [('1g', Fraction(1, 1000)), ('25g', Fraction(1, 40)), ('125g', Fraction(1, 8)),
                 ('275g', Fraction(11, 40)), ('500g', Fraction(1, 2)), ('1kg', Fraction(1)),
                 ('33cl', Fraction(33, 100)), ('250ml', Fraction(1, 4)), ('6x33cl', Fraction(198, 100)),
                 ('15x33cl', Fraction(495, 100)), ('12st', Fraction(12)), ('30p', Fraction(30)),
                 ('4-pack', Fraction(4)), ('0,5l', Fraction(1, 2))]
        for cents in (1, 5, 99, 199, 2990, 3333, 10000):
            for count in (1, 2, 3, 7):
                for size, total in packs:
                    price = f'{cents // 100},{cents % 100:02}'
                    if count > 1:
                        price = f'{count} för {price}'
                    q = app.with_unit_price(dict(sale=price), size)
                    exact_cents = Fraction(cents, count) / total
                    rounded_cents = (exact_cents + Fraction(1, 2)).numerator // (exact_cents + Fraction(1, 2)).denominator
                    displayed = q['unitPrice'].split(' kr/')[0].replace(',', '.')
                    self.assertEqual(Decimal(displayed) * 100, Decimal(rounded_cents), (price, size))

    def test_half_up_rounding_and_server_display_is_authoritative(self):
        q = app.with_unit_price(dict(sale='11,90'), '160g')
        self.assertEqual(q['unitPriceDisplay'], '74,38 kr/kilo')
        q = app.with_unit_price(dict(sale='2 för 2,01'), '1st')
        self.assertEqual(q['unitPriceDisplay'], '1,01 kr/styck')
        q = app.with_unit_price(dict(sale='1,00'), '1g')
        self.assertEqual(q['unitPriceDisplay'], '1\xa0000,00 kr/kilo')
        self.assertIn("row.unitPriceDisplay||row.unitRangeDisplay", app.PAGE)

    def test_source_conflicts_fail_closed_even_for_one_cent(self):
        for text in ('13,34/kg', '13,32/kg', '13,33/l'):
            q = app.with_unit_price(dict(sale='10 kr', unitPriceText=text), '750g')
            self.assertEqual(q['unitStatus'], 'conflict')
            self.assertNotIn('unitPrice', q)
            item = offer('10 kr', '750g', text)
            self.assertIn('Jmf-pris <strong>saknas', app.card(item, dt.date(2026, 11, 21)))

    def test_unknown_approximate_mixed_and_foreign_values_do_not_get_a_number(self):
        for price, volume in [('20 kr', 'ca500g'), ('20 kr', 'ca. 500g'), ('20 kr', '≈500g'),
                              ('20 kr', '920/450g'), ('20 kr', '750g, 750ml'), ('20 kr', ''),
                              ('från 20 kr', '500g'), ('30%', '500g'), ('20 EUR', '500g'),
                              ('-20 kr', '500g'), ('NaN', '500g'), ('2 för 20 kr/st', '500g')]:
            q = app.with_unit_price(dict(sale=price), volume)
            self.assertNotIn('unitPrice', q, (price, volume))
        for label in ('ca500g', 'ca. 500g', '200–275g', '920/450g'):
            self.assertFalse(app.label_pack_size(label), label)

    def test_ranges_are_labelled_and_never_ranked_as_single_quotes(self):
        q = app.with_unit_price(dict(sale='3 för 48', unitPriceText='106:67-128:00 kr/kg'), '125-150g')
        self.assertEqual(q['unitRangeDisplay'], '106,67–128,00 kr/kilo')
        self.assertNotIn('unitAmount', q)
        self.assertNotIn('unitPrice', q)
        item = offer('3 för 48', '125-150g', '106:67-128:00 kr/kg')
        self.assertIn('106,67–128,00 kr/kg', app.card(item, dt.date(2026, 11, 21)))
        q = app.with_unit_price(dict(sale='3 för 48', unitPriceText='100-128 kr/kg'), '125-150g')
        self.assertEqual(q['unitStatus'], 'conflict')
        q = app.with_unit_price(dict(sale='3 för 48', unitPriceText='106:67-128:00 kr/kg'), '150g')
        self.assertEqual(q['unitPriceDisplay'], '106,67 kr/kilo')

    def test_published_drained_weight_quote_and_generic_piece_field(self):
        q = app.with_unit_price(dict(sale='3 för 75', unitPriceText='250:00 kr/kg'), '275/100g')
        self.assertEqual(q['unitPriceDisplay'], '250,00 kr/kilo')
        self.assertEqual(q['unitStatus'], 'published')
        q = app.with_unit_price(dict(sale='9,90/st', unitPriceText='11:00 kr/kg'), 'Styck', 'Potatis 900g')
        self.assertEqual(q['unitPriceDisplay'], '11,00 kr/kilo')
        self.assertEqual(q['unitStatus'], 'cross-checked')

    def test_count_yield_notation_is_not_assumed_to_be_containers(self):
        for quantity in ('6p/3l', '6p 3l', '15p/33cl', '12P 33CL'):
            self.assertIsNone(app.pack_measure_decimal(quantity), quantity)
            q = app.with_unit_price(dict(sale='2 för 18'), quantity)
            self.assertNotIn('unitPrice', q)
        q = app.with_unit_price(dict(sale='2 för 18', unitPriceText='3:00/l'), '6p/3l')
        self.assertEqual(q['unitPriceDisplay'], '3,00 kr/liter')
        self.assertEqual(q['unitStatus'], 'published')
        self.assertEqual(app.pack_measure_decimal('6x33cl'), (Decimal('1.98'), 'l'))

    def test_explicit_piece_prices_are_not_invented_weights(self):
        q = app.with_unit_price(dict(sale='149 kr/st'), '', 'Förvaringskorg')
        self.assertEqual(q['unitPriceDisplay'], '149,00 kr/styck')
        self.assertEqual(q['unitScope'], 'purchase-item')
        q = app.with_unit_price(dict(sale='2 för 30 kr/st'), '', 'Strumpa')
        self.assertNotIn('unitPrice', q)
        q = app.with_unit_price(dict(sale='2 för 30', saleUnitHint='st'), '', 'Blomma')
        self.assertEqual(q['unitPriceDisplay'], '15,00 kr/styck')
        q = app.with_unit_price(dict(sale='109 kr/st'), 'Ca 1000 g', 'Stekfläsk')
        self.assertNotIn('unitPrice', q)
        q = app.with_unit_price(dict(sale='149 kr/st', unitPriceText='29,80/kg'), '', 'Matvara')
        self.assertEqual(q['unitPriceDisplay'], '29,80 kr/kilo')
        self.assertEqual(q['unitStatus'], 'published')

    def test_price_conditions_survive_normalization(self):
        q = app.with_unit_price(dict(sale='2 för 35,00', member=True, conditions=['Kupong krävs']), '500g')
        self.assertEqual(q['priceTerms'], ['Medlemspris', 'Vid köp av 2', 'Kupong'])
        q = app.with_unit_price(dict(sale='59,90/st +pant', unitPriceText='12,10/l +pant'), '15p/33cl')
        self.assertIn('Pant tillkommer', q['priceTerms'])
        q = app.with_unit_price(dict(sale='20 kr', unitPriceText='40/l', unitUncertain=True), '500ml')
        self.assertNotIn('unitPrice', q)


if __name__ == '__main__':
    unittest.main()
