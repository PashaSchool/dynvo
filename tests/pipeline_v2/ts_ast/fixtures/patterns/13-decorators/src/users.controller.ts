import { Controller, Get } from '@nestjs/common';

@Controller('users')
export class UsersController {
  @Get(':id')
  findOne(id: string): { id: string } {
    return { id };
  }

  plainList(): string[] {
    return [];
  }
}
