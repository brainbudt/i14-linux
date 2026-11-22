#define I14_KEXEC_TEST 1
#include "../tools/i14-ram-kexec.c"
#include <assert.h>
int main(void) {
 assert(layout_valid(25942024,0x1a10000,16000,233043));
 assert(layout_valid(25942024,0x1a10000,16000,302686));
 assert(!layout_valid(25942024,0x1a10000,16000,302687));
 assert(!layout_valid(0,0x1a10000,16000,233043));
 assert(!layout_valid(25942025,0x1a10000,16000,233043));
 assert(!layout_valid(25942024,0x1e00000,16000,233043));
 assert(!layout_valid(25942024,0x1a10000,0,233043));
 assert(!layout_valid(25942024,0x1a10000,65537,233043));
 assert(!layout_valid(25942024,0x1a10000,16000,233044));
 assert(!layout_valid(25942024,0x1a10000,16000,0));
 puts("PASS: fixed known kernel/initrd lengths, runtime/LK boundary and DTB capacity enforced");
}
