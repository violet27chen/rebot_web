"""单号扫描：纯图像，不需要真实设备。"""
import unittest
import cv2
import numpy as np
from backend.sorting.geometry import SortingError
from backend.sorting.label import scan, decode, destination, sharpness


def qr(text, size=300):
    img = cv2.QRCodeEncoder_create().encode(text)
    img = cv2.resize(img, (size, size), interpolation=cv2.INTER_NEAREST)
    return cv2.copyMakeBorder(img, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)


class LabelTests(unittest.TestCase):
    def test_reads_qr(self):
        r = scan(qr('SF1234567890'))
        self.assertEqual(r['status'], 'ok')
        self.assertEqual(r['codes'][0]['text'], 'SF1234567890')

    def test_blank_is_blurry_not_found(self):
        r = scan(np.full((200, 200, 3), 128, np.uint8))
        self.assertEqual(r['status'], 'blurry')
        self.assertEqual(r['codes'], [])

    def test_sharp_noise_without_code_is_not_found(self):
        noise = np.random.RandomState(0).randint(0, 255, (200, 200), np.uint8)
        self.assertEqual(scan(noise)['status'], 'not_found')

    def test_rotated_qr(self):
        img = cv2.rotate(qr('YT998877'), cv2.ROTATE_90_CLOCKWISE)
        self.assertEqual(decode(img)[0]['text'], 'YT998877')

    def test_destination_rules(self):
        rules = [{'pattern': r'SF\d+', 'destination': 'A区'}, {'pattern': r'YT\d+', 'destination': 'B区'}]
        self.assertEqual(destination([{'text': 'YT123'}], rules)['destination'], 'B区')
        with self.assertRaises(SortingError): destination([{'text': 'ZZ1'}], rules)
        with self.assertRaises(SortingError): destination([], rules)
        with self.assertRaises(SortingError): destination([{'text': 'SF1'}, {'text': 'SF2'}], rules)

    def test_invalid_image(self):
        with self.assertRaises(SortingError): decode(None)


if __name__ == '__main__':
    unittest.main()
