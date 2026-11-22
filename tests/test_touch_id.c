#define I14_TOUCH_ID_TEST 1
#include "../tools/i14-touch-id.c"
#include <assert.h>
static unsigned reads,fail_read;
static int read_register(unsigned char reg,unsigned char *v) {
 const unsigned char allowed[]={0xa3,0x9f,0xa6,0xa8};assert(reads<4 && reg==allowed[reads]);
 *v=(unsigned char)(++reads);return fail_read ? -1 : 0;
}
int main(void) {
 unsigned char v[4];assert(read_identity(v)==0 && reads==4);
 assert(v[0]==1 && v[1]==2 && v[2]==3 && v[3]==4);
 reads=0;fail_read=1;assert(read_identity(v)<0 && reads==1);
 puts("PASS: only four fixed identity register reads; first I2C error stops");
}
