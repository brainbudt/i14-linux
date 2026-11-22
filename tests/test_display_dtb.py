import importlib.util, pathlib, struct, unittest
p=pathlib.Path(__file__).resolve().parents[1]/"scripts/build-dtb-shim.py"
s=importlib.util.spec_from_file_location("display_shim",p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
class DisplayBounds(unittest.TestCase):
    def test_measured_layer_and_geometry_only(self):
        good={"reg":struct.pack(">4I",0,0xbf48e000,0,0x465000),"width":struct.pack(">I",720),"height":struct.pack(">I",1600),"stride":struct.pack(">I",2880),"format":b"a8r8g8b8\0","compatible":b"simple-framebuffer\0","status":b"okay\0"}
        self.assertEqual(m.validate_display_properties(good)["bytes"],4608000)
        for name,value in (("reg",struct.pack(">4I",0,0x44000000,0,0x465000)),("reg",struct.pack(">4I",0,0xbf48e000,0,0x464fff)),("width",struct.pack(">I",721)),("height",struct.pack(">I",1601)),("stride",struct.pack(">I",2944)),("format",b"r5g6b5\0"),("status",b"disabled\0")):
            with self.subTest(name=name,value=value),self.assertRaises(m.base.ImageError):m.validate_display_properties(dict(good,**{name:value}))
if __name__=="__main__":unittest.main()
