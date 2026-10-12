// ASS 3 - Subnetting

#include <stdio.h>

int main() {
    int a, b, c, d;

    printf("Enter IP Address: "); //192.168.10.5
    scanf("%d.%d.%d.%d", &a, &b, &c, &d);

    if (a < 0 || a > 255 ||
        b < 0 || b > 255 ||
        c < 0 || c > 255 ||
        d < 0 || d > 255) {
        printf("\nInvalid IP Address!\n");
        return 0;
    }

    if (a >= 0 && a <= 127) {
        printf("\nClass : A\n");
        printf("IP Range : 0.0.0.0 - 127.255.255.255\n\n");
        
        printf("Subnet Mask : 255.0.0.0\n");
        printf("A subnet mask is a 32-bit number used with an IP address to identify which part of the address represents the network and which part represents the host (device).\n\n");
        
        printf("\n32-bit Representation\n");
        printf("Network ID\n");
        printf("[0][N][N][N][N][N][N][N]");
        printf("[H][H][H][H][H][H][H][H]");
        printf("[H][H][H][H][H][H][H][H]");
        printf("[H][H][H][H][H][H][H][H]\n\n");
        
        printf("Hosts per Network ID : 2^24 - 2 = 16,777,214\n");
        printf("Calculation :\n");
        printf("Host bits = 24\n");
        printf("2^24 = 16,777,216\n");
        printf("Subtract 2 (Network ID and Broadcast Address)\n");
        printf("= 16,777,214 Hosts\n\n");
        
        printf("Number of Networks : 2^7 = 128\n");
        printf("Calculation :\n");
        printf("Network bits = 7\n");
        printf("2^7 = 128 Networks\n\n");

        if (a == 127)
            printf("Loopback Address : YES\n");
        else
            printf("Loopback Address : NO\n");
        printf("A loopback address is a special IP address that a computer uses to communicate with itself for testing and troubleshooting network software without sending data over a physical network.\n");
        printf("The purpose of a loopback address is to allow a computer to communicate with itself without using a physical network connection.\n");
    } else if (a >= 128 && a <= 191) {
        printf("\nClass : B\n");
        printf("IP Range : 128.0.0.0 - 191.255.255.255\n\n");
        
        printf("Default Subnet Mask : 255.255.0.0\n");
        printf("A subnet mask is a 32-bit number used with an IP address to identify which part of the address represents the network and which part represents the host (device).\n\n");
        printf("\n32-bit Representation\n");
        printf("[1][0][N][N][N][N][N][N]");
        printf("[N][N][N][N][N][N][N][N]");
        printf("[H][H][H][H][H][H][H][H]");
        printf("[H][H][H][H][H][H][H][H]\n\n");

        printf("Hosts per Network ID : 2^16 - 2 = 65,534\n");
        printf("Calculation :\n");
        printf("Host bits = 16\n");
        printf("2^16 = 65,536\n");
        printf("Subtract 2 (Network ID and Broadcast Address)\n");
        printf("= 65,534 Hosts\n\n");
        
        printf("Number of Networks : 2^14 = 16,384\n");
        printf("Calculation :\n");
        printf("Network bits = 14\n");
        printf("2^14 = 16,384 Networks\n\n");
        
        printf("Loopback Address : NO\n");
        printf("A loopback address is a special IP address that a computer uses to communicate with itself for testing and troubleshooting network software without sending data over a physical network.\n");
        printf("The purpose of a loopback address is to allow a computer to communicate with itself without using a physical network connection.\n");
    } else if (a >= 192 && a <= 223) {
        printf("\nClass : C\n");
        printf("IP Range : 192.0.0.0 - 223.255.255.255\n\n");
        
        printf("Default Subnet Mask : 255.255.255.0\n");
        printf("A subnet mask is a 32-bit number used with an IP address to identify which part of the address represents the network and which part represents the host (device).\n\n");
        
        printf("\n32-bit Representation\n");
        printf("[1][1][0][N][N][N][N][N]");
        printf("[N][N][N][N][N][N][N][N]");
        printf("[N][N][N][N][N][N][N][N]");
        printf("[H][H][H][H][H][H][H][H]\n\n");

        printf("Hosts per Network ID : 2^8 - 2 = 254\n");
        printf("Calculation :\n");
        printf("Host bits = 8\n");
        printf("2^8 = 256\n");
        printf("Subtract 2 (Network ID and Broadcast Address)\n");
        printf("= 254 Hosts\n\n");
        
        printf("Number of Networks : 2^21 = 2,097,152\n");
        printf("Calculation :\n");
        printf("Network bits = 21\n");
        printf("2^21 = 2,097,152 Networks\n\n");

        printf("Loopback Address : NO\n");
        printf("A loopback address is a special IP address that a computer uses to communicate with itself for testing and troubleshooting network software without sending data over a physical network.\n");
        printf("The purpose of a loopback address is to allow a computer to communicate with itself without using a physical network connection.\n");
    } else if (a >= 224 && a <= 239) {
        printf("\nClass : D\n");
        printf("IP Range : 224.0.0.0 - 239.255.255.255\n\n");
        
        printf("Purpose : Multicast\n\n");
        
        printf("Default Subnet Mask : Not Applicable\n");
        printf("A subnet mask is a 32-bit number used with an IP address to identify which part of the address represents the network and which part represents the host (device).\n\n");
        
        printf("Hosts per Network ID : Not Applicable\n");
        printf("Number of Networks : Not Applicable\n\n");

        printf("Loopback Address : NO\n");
        printf("A loopback address is a special IP address that a computer uses to communicate with itself for testing and troubleshooting network software without sending data over a physical network.\n");
        printf("The purpose of a loopback address is to allow a computer to communicate with itself without using a physical network connection.\n");
    } else {
        printf("\nClass : E\n");
        printf("IP Range : 240.0.0.0 - 255.255.255.255\n\n");
        
        printf("Purpose : Experimental\n\n");
        
        printf("Default Subnet Mask : Not Applicable\n");
        printf("A subnet mask is a 32-bit number used with an IP address to identify which part of the address represents the network and which part represents the host (device).\n\n");
        
        printf("Hosts per Network ID : Not Applicable\n");
        printf("Number of Networks : Not Applicable\n\n");
        
        printf("Loopback Address : NO\n");
        printf("A loopback address is a special IP address that a computer uses to communicate with itself for testing and troubleshooting network software without sending data over a physical network.\n");
        printf("The purpose of a loopback address is to allow a computer to communicate with itself without using a physical network connection.\n");
    }

    return 0;
}